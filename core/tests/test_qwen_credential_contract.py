"""WP3A public-contract tests for the Qwen credential configuration surface.

Covers the CLI and stdio MCP entries, the closed public schemas, secret
non-echo, environment independence, local-only readiness, and the isolation
guarantees that keep a credential from becoming a Cloud execution path.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import roughcut
from roughcut.adapters import qwen_credential_store as store
from roughcut.adapters.qwen_credential_store import (
    CREDENTIAL_FILENAME,
    CREDENTIAL_FORMAT_VERSION,
    CREDENTIAL_INSECURE,
    CREDENTIAL_MALFORMED,
    CREDENTIAL_NOT_CONFIGURED,
    CREDENTIAL_UNSUPPORTED_FORMAT,
    QwenCredentialError,
    credential_store_path,
)
from roughcut.application import qwen_credentials
from roughcut.application.diagnostics import diagnostics
from roughcut.application.health import SCHEMA_VERSION, TOOL_SCHEMA_VERSION, health
from roughcut.application.qwen_credentials import (
    clear_qwen_credential,
    configure_qwen_credential,
    qwen_credential_readiness,
)
from roughcut.mcp import TOOLS, handle_request

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "QWEN_SUPER_SECRET_SENTINEL_123"
WORKSPACE_ID = "fakeworkspace01"
CREDENTIAL_TOOLS = (
    "qwen_credential_configure",
    "qwen_credential_readiness",
    "qwen_credential_clear",
)


@pytest.fixture
def credential_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private temporary HOME that is never the real user home."""

    home = tmp_path / "credential home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_WORKSPACE_ID", raising=False)
    assert Path.home() == home
    return home


def child_environment(
    home: Path, overrides: dict[str, str | None] | None = None
) -> dict[str, str]:
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    environment.pop("DASHSCOPE_API_KEY", None)
    environment.pop("DASHSCOPE_WORKSPACE_ID", None)
    for key, value in (overrides or {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return environment


def run_cli(
    arguments: tuple[str, ...],
    *,
    home: Path,
    payload: str | None = None,
    environment: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roughcut.cli", *arguments],
        cwd=ROOT,
        env=child_environment(home, environment),
        input=payload,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def run_mcp(
    message: dict[str, object], *, home: Path
) -> tuple[dict[str, object] | None, str]:
    """Run one stdio MCP request per process; the server is single-business-request."""

    process = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        cwd=ROOT,
        env=child_environment(home),
        input=f"{json.dumps(message)}\n",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert process.returncode == 0
    lines = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(lines) == 1
    return lines[0], process.stderr


def mcp_call(number: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": number,
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


def created_entries(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*")
    )


# --------------------------------------------------------------------------
# frozen public identity
# --------------------------------------------------------------------------


def test_tool_schema_and_core_version_are_exact() -> None:
    payload = health()

    assert payload["schema_version"] == SCHEMA_VERSION == 1
    assert payload["tool_schema_version"] == TOOL_SCHEMA_VERSION == 32
    assert payload["core_version"] == roughcut.__version__


def test_mcp_exposes_exactly_three_closed_credential_tools() -> None:
    names = [tool["name"] for tool in TOOLS]
    assert len(names) == len(set(names))
    assert [name for name in names if "credential" in name] == list(CREDENTIAL_TOOLS)

    schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
    for name in CREDENTIAL_TOOLS:
        schema = schemas[name]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) <= {"api_key", "workspace_id"}

    configure = schemas["qwen_credential_configure"]
    assert configure["required"] == ["api_key", "workspace_id"]
    assert schemas["qwen_credential_readiness"]["properties"] == {}
    assert schemas["qwen_credential_clear"]["properties"] == {}


def test_public_contract_has_no_generic_secret_surface() -> None:
    names = [tool["name"] for tool in TOOLS]
    schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}

    assert [name for name in names if "credential" in name] == list(CREDENTIAL_TOOLS)
    assert {name.removeprefix("qwen_credential_") for name in CREDENTIAL_TOOLS} == {
        "configure",
        "readiness",
        "clear",
    }
    for forbidden in ("secret", "keyring", "vault", "registry", "_list", "_delete"):
        assert not any(forbidden in name for name in names)
    for name in CREDENTIAL_TOOLS:
        assert set(schemas[name]["properties"]) <= {"api_key", "workspace_id"}
        assert schemas[name]["additionalProperties"] is False


def test_readiness_statuses_are_closed() -> None:
    assert qwen_credentials.QWEN_CREDENTIAL_STATUSES == {
        "not_configured",
        "configured",
        "invalid",
        "insecure",
    }


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (CREDENTIAL_NOT_CONFIGURED, "not_configured"),
        (CREDENTIAL_MALFORMED, "invalid"),
        (CREDENTIAL_UNSUPPORTED_FORMAT, "invalid"),
        (CREDENTIAL_INSECURE, "insecure"),
    ],
)
def test_readiness_maps_every_closed_store_failure(
    code: str, status: str, credential_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(root: object = None) -> None:
        raise QwenCredentialError(code, "closed failure")

    monkeypatch.setattr(qwen_credentials, "read_credential", fail)

    payload = qwen_credential_readiness()

    assert payload["status"] == status
    assert payload["workspace_id_configured"] is False
    assert payload["next_action"] == "qwen_credential_configure"
    assert public_payload_leaks_nothing(payload)


def public_payload_leaks_nothing(payload: dict[str, object]) -> bool:
    rendered = json.dumps(payload, ensure_ascii=False)
    return not any(
        leak in rendered
        for leak in (str(Path.home()), CREDENTIAL_FILENAME, ".roughcut", "api_key")
    )


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------


def test_mcp_configure_readiness_clear_round_trip(credential_home: Path) -> None:
    assert mcp_call(1, "qwen_credential_readiness", {})["credential"] == {
        "provider": "qwen_filetrans",
        "status": "not_configured",
        "workspace_id_configured": False,
        "next_action": "qwen_credential_configure",
    }
    configured = mcp_call(
        2,
        "qwen_credential_configure",
        {"api_key": SENTINEL, "workspace_id": WORKSPACE_ID},
    )
    assert configured["ok"] is True
    assert configured["credential"] == {
        "provider": "qwen_filetrans",
        "status": "configured",
        "workspace_id_configured": True,
    }
    assert mcp_call(3, "qwen_credential_readiness", {})["credential"]["status"] == (
        "configured"
    )
    assert mcp_call(4, "qwen_credential_clear", {})["credential"]["status"] == (
        "not_configured"
    )
    assert mcp_call(5, "qwen_credential_clear", {})["credential"]["status"] == (
        "not_configured"
    )
    assert not credential_store_path().exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"api_key": SENTINEL},
        {"workspace_id": WORKSPACE_ID},
        {"api_key": SENTINEL, "workspace_id": WORKSPACE_ID, "extra": 1},
        {"api_key": SENTINEL, "workspace_id": WORKSPACE_ID, "path": "/tmp/x"},
    ],
)
def test_mcp_configure_rejects_every_unknown_or_incomplete_input(
    arguments: dict[str, object], credential_home: Path
) -> None:
    payload = mcp_call(1, "qwen_credential_configure", arguments)

    assert payload["ok"] is False
    assert payload["error"] == {"code": "invalid_arguments"}
    assert SENTINEL not in json.dumps(payload)
    assert not credential_store_path().exists()


@pytest.mark.parametrize("name", ["qwen_credential_readiness", "qwen_credential_clear"])
def test_mcp_argument_free_credential_tools_reject_unknown_fields(
    name: str, credential_home: Path
) -> None:
    payload = mcp_call(1, name, {"api_key": SENTINEL})

    assert payload["ok"] is False
    assert payload["error"] == {"code": "invalid_arguments"}
    assert SENTINEL not in json.dumps(payload)


@pytest.mark.parametrize(
    ("api_key", "workspace_id"),
    [
        (SENTINEL, "not a workspace"),
        ("sk\ninjected", WORKSPACE_ID),
    ],
)
def test_mcp_rejects_an_invalid_credential_shape_without_echoing_it(
    api_key: str, workspace_id: str, credential_home: Path
) -> None:
    payload = mcp_call(
        1,
        "qwen_credential_configure",
        {"api_key": api_key, "workspace_id": workspace_id},
    )

    assert payload["ok"] is False
    assert payload["error"] == {"code": "invalid_arguments"}
    assert api_key not in json.dumps(payload)
    assert not credential_store_path().exists()


def test_stdio_mcp_never_echoes_the_secret(credential_home: Path) -> None:
    def call(
        number: int, workspace_id: str
    ) -> tuple[dict[str, object] | None, str]:
        return run_mcp(
            {
                "jsonrpc": "2.0",
                "id": number,
                "method": "tools/call",
                "params": {
                    "name": "qwen_credential_configure",
                    "arguments": {"api_key": SENTINEL, "workspace_id": workspace_id},
                },
            },
            home=credential_home,
        )

    configured, stderr = call(1, WORKSPACE_ID)
    assert configured is not None
    assert SENTINEL not in json.dumps(configured)
    assert SENTINEL not in stderr
    assert configured["result"]["structuredContent"]["credential"]["status"] == (
        "configured"
    )

    invalid, stderr = call(2, "bad id")
    assert invalid is not None
    assert SENTINEL not in json.dumps(invalid)
    assert SENTINEL not in stderr
    assert invalid["result"]["isError"] is True
    assert invalid["result"]["structuredContent"]["error"] == {
        "code": "invalid_arguments"
    }

    readiness = mcp_call(3, "qwen_credential_readiness", {})
    assert json.dumps(readiness).count(SENTINEL) == 0


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def test_cli_configure_takes_the_secret_from_stdin_and_not_from_argv(
    credential_home: Path,
) -> None:
    arguments = ("qwen-credential-configure", "--json")
    assert SENTINEL not in " ".join(arguments)

    result = run_cli(
        arguments,
        home=credential_home,
        payload=json.dumps({"api_key": SENTINEL, "workspace_id": WORKSPACE_ID}),
    )

    assert result.returncode == 0
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
    assert json.loads(result.stdout)["credential"] == {
        "provider": "qwen_filetrans",
        "status": "configured",
        "workspace_id_configured": True,
    }
    assert credential_store_path().exists()


def test_cli_rejects_an_argv_secret_without_echoing_it(credential_home: Path) -> None:
    result = run_cli(
        (
            "qwen-credential-configure",
            "--api-key",
            SENTINEL,
            "--workspace-id",
            WORKSPACE_ID,
            "--json",
        ),
        home=credential_home,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == {"code": "invalid_arguments"}
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
    assert not credential_store_path().exists()


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        "[]",
        json.dumps({"api_key": SENTINEL}),
        json.dumps({"api_key": SENTINEL, "workspace_id": WORKSPACE_ID, "extra": 1}),
        json.dumps({"api_key": SENTINEL + "\x00", "workspace_id": WORKSPACE_ID}),
        json.dumps({"api_key": SENTINEL, "workspace_id": "not a workspace"}),
        '{"api_key": "'
        + SENTINEL
        + '", "workspace_id": "'
        + WORKSPACE_ID
        + '", "workspace_id": "x"}',
        json.dumps({"workspace_id": WORKSPACE_ID}),
    ],
)
def test_cli_configure_rejects_every_malformed_request_without_echo(
    payload: str, credential_home: Path
) -> None:
    result = run_cli(
        ("qwen-credential-configure", "--json"),
        home=credential_home,
        payload=payload,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == {"code": "invalid_arguments"}
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
    assert not credential_store_path().exists()


def test_cli_configure_rejects_an_oversized_request(credential_home: Path) -> None:
    payload = json.dumps(
        {"api_key": SENTINEL + "x" * 9000, "workspace_id": WORKSPACE_ID}
    )

    result = run_cli(
        ("qwen-credential-configure", "--json"),
        home=credential_home,
        payload=payload,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == {"code": "invalid_arguments"}
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
    assert not credential_store_path().exists()


def test_cli_readiness_and_clear_round_trip(credential_home: Path) -> None:
    readiness = run_cli(("qwen-credential-readiness", "--json"), home=credential_home)
    assert readiness.returncode == 0
    assert json.loads(readiness.stdout)["credential"]["status"] == "not_configured"

    assert (
        run_cli(
            ("qwen-credential-configure", "--json"),
            home=credential_home,
            payload=json.dumps({"api_key": SENTINEL, "workspace_id": WORKSPACE_ID}),
        ).returncode
        == 0
    )
    configured = run_cli(("qwen-credential-readiness", "--json"), home=credential_home)
    assert json.loads(configured.stdout)["credential"]["status"] == "configured"

    cleared = run_cli(("qwen-credential-clear", "--json"), home=credential_home)
    assert json.loads(cleared.stdout)["credential"]["status"] == "not_configured"
    cleared_again = run_cli(("qwen-credential-clear", "--json"), home=credential_home)
    assert cleared_again.returncode == 0
    assert json.loads(cleared_again.stdout)["credential"]["status"] == (
        "not_configured"
    )
    assert not credential_store_path().exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("qwen-credential-readiness", "--api-key", SENTINEL, "--json"),
        ("qwen-credential-clear", "--workspace-id", WORKSPACE_ID, "--json"),
        ("qwen-credential-readiness", "--json", "--project", "/tmp/anything"),
    ],
)
def test_cli_credential_tools_accept_only_json(
    arguments: tuple[str, ...], credential_home: Path
) -> None:
    result = run_cli(arguments, home=credential_home)

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == {"code": "invalid_arguments"}
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr


def test_cli_credential_tools_require_json_output(credential_home: Path) -> None:
    result = run_cli(("qwen-credential-readiness",), home=credential_home)

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == {"code": "json_output_required"}


# --------------------------------------------------------------------------
# environment, network and isolation
# --------------------------------------------------------------------------


def test_environment_variables_are_not_credential_truth(
    credential_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", SENTINEL)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", WORKSPACE_ID)

    payload = qwen_credential_readiness()

    assert payload["status"] == "not_configured"
    assert payload["workspace_id_configured"] is False
    assert not credential_store_path().exists()
    assert created_entries(credential_home) == []

    result = run_cli(
        ("qwen-credential-readiness", "--json"),
        home=credential_home,
        environment={
            "DASHSCOPE_API_KEY": SENTINEL,
            "DASHSCOPE_WORKSPACE_ID": WORKSPACE_ID,
        },
    )
    assert json.loads(result.stdout)["credential"]["status"] == "not_configured"
    assert SENTINEL not in result.stdout
    assert not credential_store_path().exists()


def test_readiness_is_local_only_and_opens_no_network(
    credential_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("WP3A readiness must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    assert qwen_credential_readiness()["status"] == "not_configured"
    assert (
        configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)[
            "status"
        ]
        == "configured"
    )
    assert qwen_credential_readiness()["status"] == "configured"
    assert clear_qwen_credential()["status"] == "not_configured"


def test_credential_surface_creates_no_project_operation_or_raw_asr(
    credential_home: Path,
) -> None:
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    assert created_entries(credential_home) == [
        ".roughcut",
        ".roughcut/private",
        ".roughcut/private/" + CREDENTIAL_FILENAME,
    ]
    rendered = " ".join(created_entries(credential_home))
    for forbidden in ("operations", "raw-asr", "raw_asr", "runtime.json", "project"):
        assert forbidden not in rendered

    payloads = [
        json.dumps(configure_qwen_credential(api_key="x" * 8, workspace_id=WORKSPACE_ID)),
        json.dumps(qwen_credential_readiness()),
        json.dumps(clear_qwen_credential()),
    ]
    for payload in payloads:
        for forbidden in ("media_operation", "operation_id", "transcription_failed"):
            assert forbidden not in payload


def test_never_configured_credential_creates_no_cloud_operation(
    credential_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("WP3A must never call the Qwen transport")

    monkeypatch.setattr(
        "roughcut.adapters.qwen.filetrans.run_qwen_filetrans", forbidden
    )

    assert qwen_credential_readiness()["status"] == "not_configured"
    assert clear_qwen_credential()["status"] == "not_configured"
    assert not credential_store_path().exists()

    fake_project = credential_home / "Roughcut Projects" / "demo"
    fake_project.mkdir(parents=True)
    operation_root = fake_project / "operations"
    assert not operation_root.exists()


def test_existing_local_health_and_diagnostics_are_unchanged(
    credential_home: Path,
) -> None:
    assert set(health()) == {
        "schema_version",
        "tool_schema_version",
        "core_version",
        "source_commit",
        "platform",
        "ok",
    }
    assert "credential" not in diagnostics()

    before_health = run_cli(("health", "--json"), home=credential_home)
    before_diagnostics = run_cli(("diagnostics", "--json"), home=credential_home)
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    after_health = run_cli(("health", "--json"), home=credential_home)
    after_diagnostics = run_cli(("diagnostics", "--json"), home=credential_home)

    assert before_health.stdout == after_health.stdout
    assert before_diagnostics.stdout == after_diagnostics.stdout
    assert "credential" not in json.loads(after_diagnostics.stdout)
    assert SENTINEL not in after_diagnostics.stdout


def test_configured_state_does_not_start_cloud_transcription(
    credential_home: Path,
) -> None:
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    payload = mcp_call(1, "qwen_credential_readiness", {})["credential"]

    assert payload["status"] == "configured"
    assert "enabled" not in json.dumps(payload)
    assert "cloud" not in json.dumps(payload).lower()
    assert not (credential_home / ".roughcut" / "raw-asr").exists()


def test_public_responses_never_return_the_store_path_or_the_secret(
    credential_home: Path,
) -> None:
    rendered = [
        json.dumps(
            configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
        ),
        json.dumps(qwen_credential_readiness()),
        json.dumps(clear_qwen_credential()),
    ]

    for payload in rendered:
        assert SENTINEL not in payload
        assert WORKSPACE_ID not in payload
        assert str(credential_home) not in payload
        assert CREDENTIAL_FILENAME not in payload
        assert "api_key" not in payload


def test_store_format_version_is_pinned() -> None:
    assert CREDENTIAL_FORMAT_VERSION == 1
    assert store.CREDENTIAL_PROVIDER == "qwen_filetrans"
    assert store.PRIVATE_DIRECTORY_MODE == 0o700
    assert store.PRIVATE_FILE_MODE == 0o600
    assert store.QWEN_CREDENTIAL_ERROR_CODES == {
        "not_configured",
        "invalid",
        "unsupported_format",
        "malformed",
        "insecure",
        "write_failed",
    }


def test_cli_and_mcp_clear_remove_a_crash_leftover_staging_copy(
    credential_home: Path,
) -> None:
    assert (
        run_cli(
            ("qwen-credential-configure", "--json"),
            home=credential_home,
            payload=json.dumps({"api_key": SENTINEL, "workspace_id": WORKSPACE_ID}),
        ).returncode
        == 0
    )
    directory = credential_store_path().parent
    staging = directory / ".qwen-filetrans.abandoned.tmp"
    staging.write_text(SENTINEL, encoding="utf-8")

    # A leftover staging copy inside the verified private directory is cleanup
    # residue rather than a confidentiality breach, so the intact canonical
    # record still decides readiness; the public clear removes the residue.
    readiness = run_cli(("qwen-credential-readiness", "--json"), home=credential_home)
    assert json.loads(readiness.stdout)["credential"]["status"] == "configured"
    assert SENTINEL not in readiness.stdout

    cleared = run_cli(("qwen-credential-clear", "--json"), home=credential_home)
    assert cleared.returncode == 0
    assert json.loads(cleared.stdout)["credential"]["status"] == "not_configured"
    assert not staging.exists()
    assert not credential_store_path().exists()

    staging.write_text(SENTINEL, encoding="utf-8")
    payload = mcp_call(1, "qwen_credential_readiness", {})
    assert payload["credential"]["status"] == "not_configured"
    payload = mcp_call(2, "qwen_credential_clear", {})
    assert payload["credential"]["status"] == "not_configured"
    assert not staging.exists()
    leaked = [
        path
        for path in credential_home.rglob("*")
        if path.is_file()
        and SENTINEL in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == []
