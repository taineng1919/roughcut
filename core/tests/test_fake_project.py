from __future__ import annotations

from pathlib import Path

from roughcut import __version__
from roughcut.application.fake_projects import fake_project_roundtrip
from roughcut.application.health import (
    SCHEMA_VERSION,
    TOOL_SCHEMA_VERSION,
    health,
    source_commit,
)


def test_fake_project_roundtrip_returns_the_serialized_project() -> None:
    result = fake_project_roundtrip("contract fixture")

    assert result == {
        "schema_version": SCHEMA_VERSION,
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "core_version": __version__,
        "source_commit": source_commit(),
        "ok": True,
        "project": {"schema_version": SCHEMA_VERSION, "name": "contract fixture"},
    }


def test_fake_project_carries_the_same_source_commit_as_health() -> None:
    import roughcut.application.fake_projects as fake_module
    import roughcut.application.health as health_module

    # One shared authority, not a duplicated Git/SHA parser.
    assert fake_module.source_commit is health_module.source_commit
    result = fake_project_roundtrip("contract fixture")
    assert result["source_commit"] == health()["source_commit"]
    assert result["source_commit"] is None  # dev checkout tracks None
    assert SCHEMA_VERSION == 1
    assert TOOL_SCHEMA_VERSION == 32


def test_transports_do_not_drop_source_commit() -> None:
    import json as json_module

    from roughcut.mcp import _tool_result

    result = fake_project_roundtrip("contract fixture")
    expected = health()["source_commit"]
    # CLI writes the application dict as JSON unchanged.
    assert json_module.loads(json_module.dumps(result))["source_commit"] == expected
    # MCP wraps the application dict unchanged in structuredContent.
    assert _tool_result(result)["structuredContent"]["source_commit"] == expected


def test_fake_project_roundtrip_rejects_an_empty_name() -> None:
    try:
        fake_project_roundtrip("")
    except ValueError as error:
        assert str(error) == "project_name is required"
    else:
        raise AssertionError("an empty project name must be rejected")


def test_fake_project_roundtrip_removes_its_temporary_project(tmp_path: Path) -> None:
    fake_project_roundtrip("contract fixture", temporary_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []
