from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import roughcut
from roughcut.application.health import SCHEMA_VERSION, TOOL_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_python_39_is_explicitly_rejected() -> None:
    try:
        roughcut._ensure_supported_python((3, 9))
    except RuntimeError as error:
        assert str(error) == "roughcut requires Python 3.11 or newer"
    else:
        raise AssertionError("Python 3.9 must be rejected")


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roughcut.cli", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def test_health_json_reports_version_and_platform() -> None:
    result = run_cli("health", "--json")

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["tool_schema_version"] == TOOL_SCHEMA_VERSION == 32
    assert payload["core_version"] == roughcut.__version__
    assert payload["platform"]
    assert payload["ok"] is True
    assert "error" not in payload


def test_cli_errors_are_versioned_json() -> None:
    assert_json_error(("unknown", "--json"), "unknown_command")


def test_argument_parse_errors_are_versioned_json() -> None:
    assert_json_error(("health", "--unknown", "--json"), "invalid_arguments")
    assert_json_error(("health", "unexpected-project", "--json"), "invalid_arguments")
    assert_json_error(("fake-project-roundtrip", "--project-name", "--json"), "invalid_arguments")
    assert_json_error(("health",), "json_output_required")


def assert_json_error(arguments: tuple[str, ...], error_code: str) -> None:
    result = run_cli(*arguments)

    assert result.returncode == 2
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == {"code": error_code}
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["tool_schema_version"] == TOOL_SCHEMA_VERSION == 32
    assert payload["core_version"] == roughcut.__version__
    assert payload["platform"]
