from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from windows.w4_support import (
    CORE_ROOT,
    McpChild,
    child_environment,
    structured_payload,
    tool_call,
    wait_for_file,
)

from roughcut.application.health import TOOL_SCHEMA_VERSION, health

_BLOCKING_SCRIPT = """
import os
import time
from pathlib import Path
import roughcut.mcp as mcp

original = mcp.handle_request
entered = Path(os.environ["ROUGHCUT_W4_ENTERED"])
release = Path(os.environ["ROUGHCUT_W4_RELEASE"])
completed = Path(os.environ["ROUGHCUT_W4_COMPLETED"])

def blocking(request):
    params = request.get("params", {})
    arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
    if (
        request.get("method") == "tools/call"
        and params.get("name") == "health"
        and arguments.get("_w4_block") is True
    ):
        entered.write_text("1", encoding="utf-8")
        while not release.exists():
            time.sleep(0.01)
        forwarded = dict(request)
        forwarded["params"] = {"name": "health", "arguments": {}}
        response = original(forwarded)
        completed.write_text("1", encoding="utf-8")
        return response
    return original(request)

mcp.handle_request = blocking
mcp.main()
"""


def _result_payload(response: dict[str, object]) -> dict[str, object]:
    result = response.get("result")
    assert isinstance(result, dict), response
    return result


def test_stdio_initialize_ping_and_tools_call_use_utf8_json_pipes() -> None:
    with McpChild.start(environment={"PYTHONUTF8": "0"}) as child:
        child.send(
            {
                "jsonrpc": "2.0",
                "id": "initialize",
                "method": "initialize",
                "params": {},
            }
        )
        initialize = child.read_json()
        assert _result_payload(initialize)["protocolVersion"] == "2025-03-26"

        child.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
        )
        child.send({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
        assert child.read_json() == {
            "jsonrpc": "2.0",
            "id": "ping",
            "result": {},
        }

        child.send(
            tool_call(
                "fake",
                "fake_project_roundtrip",
                {"project_name": "中文 MCP project with spaces"},
            )
        )
        fake_response = child.read_json()
        fake_result = _result_payload(fake_response)
        payload = structured_payload(fake_response)
        assert payload["ok"] is True
        assert payload["project"]["name"] == "中文 MCP project with spaces"
        assert json.loads(fake_result["content"][0]["text"])["project"]["name"] == (
            "中文 MCP project with spaces"
        )

        child.send(tool_call("health", "health", {}))
        health_response = child.read_json()
        assert structured_payload(health_response) == health()
        assert health_response["result"]["structuredContent"]["tool_schema_version"] == (
            TOOL_SCHEMA_VERSION
        )

        child.close_stdin()
        returncode, stderr = child.wait()
        assert returncode == 0
        assert stderr == b""


@pytest.mark.skipif(
    os.name != "nt",
    reason="the cp1252 stdio locale variant requires a real Windows pipe",
)
def test_stdio_unicode_input_survives_a_non_utf8_windows_stdio_locale() -> None:
    with McpChild.start(
        environment={"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"}
    ) as child:
        child.send(
            tool_call(
                "fake-windows-locale",
                "fake_project_roundtrip",
                {"project_name": "中文 Windows locale"},
            )
        )
        response = child.read_json()
        assert structured_payload(response)["project"]["name"] == "中文 Windows locale"
        child.close_stdin()
        returncode, stderr = child.wait()
        assert returncode == 0
        assert stderr == b""


def test_stdio_long_call_keeps_ping_live_and_returns_busy_for_second_business_call(
    tmp_path: Path,
) -> None:
    entered = tmp_path / "中文 entered with spaces"
    release = tmp_path / "中文 release with spaces"
    completed = tmp_path / "中文 completed with spaces"
    environment = {
        "ROUGHCUT_W4_ENTERED": str(entered),
        "ROUGHCUT_W4_RELEASE": str(release),
        "ROUGHCUT_W4_COMPLETED": str(completed),
    }
    with McpChild.start(script=_BLOCKING_SCRIPT, environment=environment) as child:
        child.send(tool_call("long", "health", {"_w4_block": True}))
        wait_for_file(entered)

        child.send({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
        child.send(tool_call("busy", "health", {}))
        responses = [child.read_json(timeout=2) for _ in range(2)]
        by_id = {response["id"]: response for response in responses}
        assert by_id["ping"] == {
            "jsonrpc": "2.0",
            "id": "ping",
            "result": {},
        }
        assert by_id["busy"] == {
            "jsonrpc": "2.0",
            "id": "busy",
            "error": {"code": -32000, "message": "Server busy"},
        }

        release.write_text("1", encoding="utf-8")
        wait_for_file(completed)
        long_response = child.read_json(timeout=2)
        assert long_response["id"] == "long"
        assert structured_payload(long_response) == health()

        child.close_stdin()
        returncode, stderr = child.wait()
        assert returncode == 0
        assert stderr == b""


@pytest.mark.parametrize(
    ("cancel_id", "expect_original_response"),
    (("long", False), ("other", True)),
)
def test_stdio_cancel_only_abandons_matching_late_response(
    tmp_path: Path,
    cancel_id: str,
    expect_original_response: bool,
) -> None:
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    completed = tmp_path / "completed"
    environment = {
        "ROUGHCUT_W4_ENTERED": str(entered),
        "ROUGHCUT_W4_RELEASE": str(release),
        "ROUGHCUT_W4_COMPLETED": str(completed),
    }
    with McpChild.start(script=_BLOCKING_SCRIPT, environment=environment) as child:
        child.send(tool_call("long", "health", {"_w4_block": True}))
        wait_for_file(entered)
        child.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": cancel_id},
            }
        )
        child.send({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
        assert child.read_json(timeout=2) == {
            "jsonrpc": "2.0",
            "id": "ping",
            "result": {},
        }

        release.write_text("1", encoding="utf-8")
        wait_for_file(completed)
        if expect_original_response:
            original = child.read_json(timeout=2)
            assert original["id"] == "long"
            assert structured_payload(original) == health()
        else:
            succeeded = False
            for attempt in range(10):
                request_id = f"after-{attempt}"
                child.send(tool_call(request_id, "health", {}))
                after = child.read_json(timeout=1)
                assert after["id"] != "long"
                if "result" in after:
                    assert after["id"] == request_id
                    assert structured_payload(after) == health()
                    succeeded = True
                    break
                assert after == {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": "Server busy"},
                }
            assert succeeded

        child.close_stdin()
        returncode, stderr = child.wait()
        assert returncode == 0
        assert stderr == b""


def test_stdio_eof_closes_the_production_server_cleanly() -> None:
    with McpChild.start() as child:
        child.send({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
        assert child.read_json() == {
            "jsonrpc": "2.0",
            "id": "ping",
            "result": {},
        }
        child.close_stdin()
        returncode, stderr = child.wait()
        assert returncode == 0
        assert stderr == b""


@pytest.mark.parametrize(
    ("exception_name", "expected_returncode", "expect_stderr"),
    (("SystemExit", 73, False), ("KeyboardInterrupt", None, True)),
)
def test_stdio_business_base_exceptions_are_not_json_error_responses(
    exception_name: str,
    expected_returncode: int | None,
    expect_stderr: bool,
) -> None:
    script = f"""
import roughcut.mcp as mcp

def fail(request):
    if request.get("method") == "tools/call":
        raise {exception_name}({expected_returncode or ''})
    return mcp.handle_request(request)

mcp.handle_request = fail
mcp.main()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=CORE_ROOT,
        input=(
            json.dumps(tool_call("exception", "health", {}), ensure_ascii=False).encode(
                "utf-8"
            )
            + b"\n"
        ),
        check=False,
        capture_output=True,
        text=False,
        env=child_environment(),        timeout=5,
    )
    assert completed.stdout == b""
    if expected_returncode is not None:
        assert completed.returncode == expected_returncode
        assert completed.stderr == b""
    else:
        assert completed.returncode != 0
        assert expect_stderr
        assert b"KeyboardInterrupt" in completed.stderr
