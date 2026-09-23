from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import roughcut.mcp
from roughcut.application.diagnostics import diagnostics
from roughcut.application.fake_projects import fake_project_roundtrip
from roughcut.application.health import TOOL_SCHEMA_VERSION, health
from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import MediaOperationError
from roughcut.mcp import TOOLS


def exchange(messages: list[dict[str, object]]) -> tuple[list[dict[str, object]], str]:
    process = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input="".join(f"{json.dumps(message)}\n" for message in messages),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert process.returncode == 0
    return [json.loads(line) for line in process.stdout.splitlines()], process.stderr


def tool_payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


def cli_fake_project(project_name: str) -> dict[str, object]:
    process = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", "fake-project-roundtrip", "--project-name", project_name, "--json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert process.returncode == 0
    assert process.stderr == ""
    return json.loads(process.stdout)


def test_mcp_health_is_the_same_application_result_as_cli() -> None:
    responses, stderr = exchange(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "health"}},
        ]
    )

    assert stderr == ""
    assert responses[0]["result"] == {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "roughcut", "version": health()["core_version"]},
    }
    assert tool_payload(responses[1]) == health()
    result = responses[1]["result"]
    assert isinstance(result, dict)
    assert json.loads(result["content"][0]["text"]) == health()


def test_mcp_preserves_workflow_error_detail_in_content_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "Roughcut workflow façade rejected the fixture: exact evidence"

    def fail(*_args: object, **_kwargs: object) -> object:
        raise WorkflowError("workflow_fixture", detail)

    monkeypatch.setattr(roughcut.mcp, "workflow_start", fail)
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "workflow-error",
            "method": "tools/call",
            "params": {
                "name": "workflow_start",
                "arguments": {
                    "project_path": "fixture-project",
                    "run_id": "run_fixture",
                    "ordered_source_ids": ["source_fixture"],
                },
            },
        }
    )

    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {"code": "workflow_fixture"}
    content_payload = json.loads(result["content"][0]["text"])
    assert content_payload["error"] == {
        "code": "workflow_fixture",
        "message": detail,
    }


def test_mcp_keeps_media_error_content_opaque(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "media implementation detail must stay hidden"

    def fail(*_args: object, **_kwargs: object) -> object:
        raise MediaOperationError("operation_integrity_error", detail)

    monkeypatch.setattr(roughcut.mcp, "workflow_start", fail)
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "media-error",
            "method": "tools/call",
            "params": {
                "name": "workflow_start",
                "arguments": {
                    "project_path": "fixture-project",
                    "run_id": "run_fixture",
                    "ordered_source_ids": ["source_fixture"],
                },
            },
        }
    )

    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"]["error"] == {
        "code": "operation_integrity_error"
    }
    content_text = result["content"][0]["text"]
    assert json.loads(content_text)["error"] == {"code": "operation_integrity_error"}
    assert detail not in content_text


def test_stdio_notifications_produce_no_response() -> None:
    responses, stderr = exchange(
        [
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": "absent"},
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/fixture-unknown",
                "params": {},
            },
            {"jsonrpc": "2.0", "id": "ping", "method": "ping"},
        ]
    )

    assert responses == [{"jsonrpc": "2.0", "id": "ping", "result": {}}]
    assert stderr == ""


@pytest.mark.parametrize(
    ("cancelled_request_id", "expect_original_response"),
    (("long", False), ("other", True)),
)
def test_stdio_cancellation_only_abandons_exact_active_response(
    tmp_path: Path,
    cancelled_request_id: str,
    expect_original_response: bool,
) -> None:
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    completed = tmp_path / "completed"
    script = """
import os
import time
from pathlib import Path
import roughcut.mcp as mcp

original = mcp.handle_request
entered = Path(os.environ["ROUGHCUT_TEST_ENTERED"])
release = Path(os.environ["ROUGHCUT_TEST_RELEASE"])
completed = Path(os.environ["ROUGHCUT_TEST_COMPLETED"])

def blocking(request):
    params = request.get("params", {})
    arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
    if (
        request.get("method") == "tools/call"
        and params.get("name") == "health"
        and arguments.get("_test_block") is True
    ):
        entered.write_text("1", encoding="utf-8")
        while not release.exists():
            time.sleep(0.01)
        request = dict(request)
        request["params"] = {"name": "health", "arguments": {}}
        response = original(request)
        completed.write_text("1", encoding="utf-8")
        return response
    return original(request)

mcp.handle_request = blocking
mcp.main()
"""
    environment = dict(os.environ)
    environment["ROUGHCUT_TEST_ENTERED"] = str(entered)
    environment["ROUGHCUT_TEST_RELEASE"] = str(release)
    environment["ROUGHCUT_TEST_COMPLETED"] = str(completed)
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: [lines.put(line) for line in process.stdout],
        name="test-mcp-cancel-stdout-reader",
    )
    reader.start()

    def send(message: dict[str, object]) -> None:
        process.stdin.write(f"{json.dumps(message)}\n")
        process.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": "long",
                "method": "tools/call",
                "params": {
                    "name": "health",
                    "arguments": {"_test_block": True},
                },
            }
        )
        deadline = time.monotonic() + 2
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.read_text(encoding="utf-8") == "1"

        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": cancelled_request_id},
            }
        )
        send({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
        assert json.loads(lines.get(timeout=1)) == {
            "jsonrpc": "2.0",
            "id": "ping",
            "result": {},
        }

        release.write_text("", encoding="utf-8")
        deadline = time.monotonic() + 2
        while not completed.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert completed.read_text(encoding="utf-8") == "1"

        if expect_original_response:
            original_response = json.loads(lines.get(timeout=1))
            assert original_response["id"] == "long"
            assert tool_payload(original_response) == health()
        else:
            next_request = 0
            followup_succeeded = False
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                request_id = f"after-{next_request}"
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "health", "arguments": {}},
                    }
                )
                observed = json.loads(lines.get(timeout=1))
                assert observed["id"] != "long"
                if "result" in observed:
                    assert observed["id"] == request_id
                    assert tool_payload(observed) == health()
                    followup_succeeded = True
                    break
                assert observed == {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": "Server busy"},
                }
                next_request += 1
            assert followup_succeeded

        process.stdin.close()
        assert process.wait(timeout=2) == 0
        reader.join(timeout=2)
        assert not reader.is_alive()
        assert lines.empty()
        assert process.stderr.read() == ""
    finally:
        release.write_text("", encoding="utf-8")
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        reader.join(timeout=2)


def test_stdio_long_call_keeps_ping_live_and_rejects_second_business_call(
    tmp_path: Path,
) -> None:
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    script = """
import os
import time
from pathlib import Path
import roughcut.mcp as mcp

original = mcp.handle_request
entered = Path(os.environ["ROUGHCUT_TEST_ENTERED"])
release = Path(os.environ["ROUGHCUT_TEST_RELEASE"])

def blocking(request):
    params = request.get("params", {})
    arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
    if (
        request.get("method") == "tools/call"
        and params.get("name") == "health"
        and arguments.get("_test_block") is True
    ):
        entered.write_text("1", encoding="utf-8")
        while not release.exists():
            time.sleep(0.01)
        request = dict(request)
        request["params"] = {"name": "health", "arguments": {}}
    return original(request)

mcp.handle_request = blocking
mcp.main()
"""
    environment = dict(os.environ)
    environment["ROUGHCUT_TEST_ENTERED"] = str(entered)
    environment["ROUGHCUT_TEST_RELEASE"] = str(release)
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: [lines.put(line) for line in process.stdout],
        name="test-mcp-stdout-reader",
    )
    reader.start()

    def send(message: dict[str, object]) -> None:
        process.stdin.write(f"{json.dumps(message)}\n")
        process.stdin.flush()

    send(
        {
            "jsonrpc": "2.0",
            "id": "long",
            "method": "tools/call",
            "params": {
                "name": "health",
                "arguments": {"_test_block": True},
            },
        }
    )
    deadline = time.monotonic() + 2
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entered.read_text(encoding="utf-8") == "1"

    send({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
    send(
        {
            "jsonrpc": "2.0",
            "id": "busy",
            "method": "tools/call",
            "params": {"name": "health", "arguments": {}},
        }
    )
    ping = json.loads(lines.get(timeout=1))
    busy = json.loads(lines.get(timeout=1))
    assert ping == {"jsonrpc": "2.0", "id": "ping", "result": {}}
    assert busy == {
        "jsonrpc": "2.0",
        "id": "busy",
        "error": {"code": -32000, "message": "Server busy"},
    }
    assert entered.read_text(encoding="utf-8") == "1"

    release.write_text("", encoding="utf-8")
    original = json.loads(lines.get(timeout=2))
    assert original["id"] == "long"
    assert tool_payload(original) == health()

    process.stdin.close()
    assert process.wait(timeout=2) == 0
    reader.join(timeout=2)
    assert not reader.is_alive()
    assert lines.empty()
    assert process.stderr.read() == ""


def test_stdio_business_system_exit_terminates_process() -> None:
    script = """
import roughcut.mcp as mcp

original = mcp.handle_request

def exiting(request):
    if request.get("method") == "tools/call":
        raise SystemExit(73)
    return original(request)

mcp.handle_request = exiting
mcp.main()
"""
    process = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "exit",
                "method": "tools/call",
                "params": {"name": "health", "arguments": {}},
            }
        )
        + "\n",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=2,
    )

    assert process.returncode == 73
    assert process.stdout == ""
    assert process.stderr == ""


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [(SystemExit(74), 74), (KeyboardInterrupt(), None)],
)
def test_stdio_reader_base_exception_is_rethrown_from_main(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_code: int | None,
) -> None:
    class _FailingStdin:
        def __iter__(self) -> _FailingStdin:
            return self

        def __next__(self) -> str:
            raise failure

    business_calls: list[dict[str, object]] = []

    def unexpected_business_call(request: dict[str, object]) -> object:
        business_calls.append(request)
        raise AssertionError("reader failure must occur before business dispatch")

    monkeypatch.setattr(roughcut.mcp.sys, "stdin", _FailingStdin())
    monkeypatch.setattr(roughcut.mcp, "handle_request", unexpected_business_call)

    with pytest.raises(type(failure)) as raised:
        roughcut.mcp.main()

    assert raised.value is failure
    assert business_calls == []
    if expected_code is not None:
        assert isinstance(raised.value, SystemExit)
        assert raised.value.code == expected_code


@pytest.mark.skipif(os.name == "nt", reason="SIGINT process semantics are POSIX-only")
def test_stdio_sigint_interrupts_blocking_business_without_release(
    tmp_path: Path,
) -> None:
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    script = """
import os
import time
from pathlib import Path
import roughcut.mcp as mcp

original = mcp.handle_request
entered = Path(os.environ["ROUGHCUT_TEST_ENTERED"])

def blocking(request):
    if request.get("method") == "tools/call":
        entered.write_text("1", encoding="utf-8")
        while True:
            time.sleep(1)
    return original(request)

mcp.handle_request = blocking
mcp.main()
"""
    environment = dict(os.environ)
    environment["ROUGHCUT_TEST_ENTERED"] = str(entered)
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
    )
    assert process.stdin is not None
    try:
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "interrupt",
                    "method": "tools/call",
                    "params": {"name": "health", "arguments": {}},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        deadline = time.monotonic() + 2
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.read_text(encoding="utf-8") == "1"

        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=2) != 0
        stdout, _stderr = process.communicate(timeout=1)
        assert stdout == ""
        assert not release.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_mcp_diagnostics_is_the_same_application_result_as_cli() -> None:
    responses, stderr = exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "diagnostics"},
            },
        ]
    )

    assert stderr == ""
    assert tool_payload(responses[0]) == diagnostics()


def test_mcp_lists_the_versioned_project_and_transcript_tools() -> None:
    responses, stderr = exchange(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}]
    )

    assert stderr == ""
    result = responses[0]["result"]
    assert isinstance(result, dict)
    assert result["toolSchemaVersion"] == TOOL_SCHEMA_VERSION == 32
    assert {tool["name"] for tool in TOOLS} >= {
        "diagnostics",
        "workflow_start",
        "workflow_status",
        "media_operation_status",
        "workflow_action",
        "workflow_cancel",
        "project_create",
        "source_add",
        "transcribe_source",
        "transcript_page",
        "brief_create",
        "brief_read",
        "agent_context",
        "revision_context",
        "proposal_create",
        "proposal_diff_read",
        "proposal_confirm",
        "proposal_reject",
        "decision_read",
        "edit_decision_read",
        "edit_change",
        "edit_undo",
        "edit_redo",
        "edit_history_read",
        "render_roughcut",
    }


def test_all_mcp_tools_advertise_a_top_level_object_input_schema() -> None:
    for tool in TOOLS:
        assert tool["inputSchema"]["type"] == "object", tool["name"]


def test_mcp_fake_project_roundtrip_is_the_application_result() -> None:
    responses, stderr = exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": "fake",
                "method": "tools/call",
                "params": {
                    "name": "fake_project_roundtrip",
                    "arguments": {"project_name": "contract fixture"},
                },
            }
        ]
    )

    assert stderr == ""
    expected = cli_fake_project("contract fixture")
    assert expected == fake_project_roundtrip("contract fixture")
    assert tool_payload(responses[0]) == expected
