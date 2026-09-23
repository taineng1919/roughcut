from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import roughcut.cli
import roughcut.mcp
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.people import PeopleMutation, PeopleState
from roughcut.application.projects import create_project
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import ProjectError


def _state() -> PeopleState:
    person = Person(person_id="person_fixture", name="嘉宾", role="guest", note="")
    return PeopleState(
        project_id="proj_fixture",
        project_revision=4,
        persons=(person,),
        sources=(
            {
                "source_id": "src_fixture",
                "display_name": "fixture.wav",
                "tags": ["访谈"],
                "note": "主素材",
            },
        ),
        speaker_maps=(
            SpeakerMap(
                source_id="src_fixture",
                transcript_version_id="tr_fixture",
                local_speaker_id="spk_0",
                person_id=person.person_id,
                confirmed_by_user=True,
            ),
        ),
    )


def _mutation(change: str = "updated") -> PeopleMutation:
    return PeopleMutation(change=change, changed=True, state=_state())


def _mcp_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


def test_people_tools_remain_listed_at_current_schema_version() -> None:
    assert TOOL_SCHEMA_VERSION == 32
    assert {tool["name"] for tool in roughcut.mcp.TOOLS} >= {
        "person_create",
        "source_metadata_update",
        "speaker_map_confirm",
        "people_read",
    }


def test_cli_person_create_and_mcp_people_read_use_real_services(tmp_path: Path) -> None:
    project_path = tmp_path / "people contract"
    create_project(project_path, "People contract")
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "person-create",
            "--project",
            str(project_path),
            "--name",
            "嘉宾",
            "--role",
            "guest",
            "--note",
            "",
            "--expected-revision",
            "0",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0
    assert cli.stderr == ""
    created = json.loads(cli.stdout)
    assert created["change"] == "created"
    assert created["people"]["project_revision"] == 1

    read = _mcp_call("people_read", {"project_path": str(project_path)})
    assert read["people"] == created["people"]


def test_cli_and_mcp_source_metadata_update_pass_identical_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_update(
        project_path: Path,
        *,
        source_id: str,
        display_name: str | None,
        tags: list[str],
        note: str,
        expected_revision: int,
    ) -> PeopleMutation:
        calls.append(
            {
                "project_path": project_path,
                "source_id": source_id,
                "display_name": display_name,
                "tags": tags,
                "note": note,
                "expected_revision": expected_revision,
            }
        )
        return _mutation()

    monkeypatch.setattr(roughcut.cli, "update_source_metadata", fake_update)
    roughcut.cli.main(
        [
            "source-metadata-update",
            "--project",
            str(tmp_path / "project"),
            "--source-id",
            "src_fixture",
            "--tags-json",
            '["访谈", "嘉宾"]',
            "--note",
            "主素材",
            "--expected-revision",
            "3",
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(roughcut.mcp, "update_source_metadata", fake_update)
    mcp_payload = _mcp_call(
        "source_metadata_update",
        {
            "project_path": str(tmp_path / "project"),
            "source_id": "src_fixture",
            "tags": ["访谈", "嘉宾"],
            "note": "主素材",
            "expected_revision": 3,
        },
    )

    assert calls[0] == calls[1]
    assert calls[0]["display_name"] is None
    assert cli_payload == mcp_payload


def test_cli_and_mcp_source_metadata_update_accept_optional_display_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_update(project_path: Path, **arguments: object) -> PeopleMutation:
        calls.append({"project_path": project_path, **arguments})
        return _mutation()

    monkeypatch.setattr(roughcut.cli, "update_source_metadata", fake_update)
    roughcut.cli.main(
        [
            "source-metadata-update",
            "--project",
            str(tmp_path / "project"),
            "--source-id",
            "src_fixture",
            "--display-name",
            "校长 开场",
            "--tags-json",
            '["role:main", "content:talk"]',
            "--note",
            "主线",
            "--expected-revision",
            "3",
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(roughcut.mcp, "update_source_metadata", fake_update)
    mcp_payload = _mcp_call(
        "source_metadata_update",
        {
            "project_path": str(tmp_path / "project"),
            "source_id": "src_fixture",
            "display_name": "校长 开场",
            "tags": ["role:main", "content:talk"],
            "note": "主线",
            "expected_revision": 3,
        },
    )

    assert calls[0] == calls[1]
    assert calls[0]["display_name"] == "校长 开场"
    assert cli_payload == mcp_payload


def test_cli_and_mcp_speaker_confirmation_pass_identical_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_confirm(project_path: Path, **arguments: object) -> PeopleMutation:
        calls.append({"project_path": project_path, **arguments})
        return _mutation("created")

    common = {
        "source_id": "src_fixture",
        "transcript_version_id": "tr_fixture",
        "local_speaker_id": "spk_0",
        "person_id": "person_fixture",
        "confirmed_by_user": True,
        "expected_revision": 3,
    }
    monkeypatch.setattr(roughcut.cli, "confirm_speaker_map", fake_confirm)
    roughcut.cli.main(
        [
            "speaker-map-confirm",
            "--project",
            str(tmp_path / "project"),
            "--source-id",
            "src_fixture",
            "--transcript-id",
            "tr_fixture",
            "--local-speaker-id",
            "spk_0",
            "--person-id",
            "person_fixture",
            "--confirmed-by-user",
            "true",
            "--expected-revision",
            "3",
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(roughcut.mcp, "confirm_speaker_map", fake_confirm)
    mcp_payload = _mcp_call(
        "speaker_map_confirm",
        {"project_path": str(tmp_path / "project"), **common},
    )

    assert calls[0] == calls[1]
    assert cli_payload == mcp_payload


def test_people_cli_and_mcp_failures_keep_versioned_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> PeopleMutation:
        raise ProjectError("fixture failure")

    monkeypatch.setattr(roughcut.cli, "create_person", fail)
    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "person-create",
                "--project",
                str(tmp_path / "project"),
                "--name",
                "嘉宾",
                "--role",
                "guest",
                "--note",
                "",
                "--expected-revision",
                "0",
                "--json",
            ]
        )
    streams = capsys.readouterr()
    assert exit_info.value.code == 2
    assert streams.err == ""
    assert len(streams.out.splitlines()) == 1
    assert json.loads(streams.out)["error"] == {"code": "people_operation_failed"}

    monkeypatch.setattr(roughcut.mcp, "confirm_speaker_map", fail)
    payload = _mcp_call(
        "speaker_map_confirm",
        {
            "project_path": str(tmp_path / "project"),
            "source_id": "src_fixture",
            "transcript_version_id": "tr_fixture",
            "local_speaker_id": "spk_0",
            "person_id": "person_fixture",
            "confirmed_by_user": False,
            "expected_revision": 0,
        },
    )
    assert payload["ok"] is False
    assert payload["error"] == {"code": "people_operation_failed"}


def test_people_cli_rejects_invalid_tags_as_one_json_object() -> None:
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "source-metadata-update",
            "--project",
            "fixture",
            "--source-id",
            "src_fixture",
            "--tags-json",
            "not-json",
            "--note",
            "",
            "--expected-revision",
            "0",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 2
    assert process.stderr == ""
    assert len(process.stdout.splitlines()) == 1
    assert json.loads(process.stdout)["error"] == {"code": "people_operation_failed"}
