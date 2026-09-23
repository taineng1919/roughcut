"""Build a temporary Host Package from canonical repository sources."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
HOST_INTEGRATIONS = {
    "codex": ROOT / "host-integrations" / "codex",
    "workbuddy": ROOT / "host-integrations" / "workbuddy",
}
VALIDATOR = ROOT / "scripts" / "validate_agent_skills.py"


def validate_sources(host: str) -> None:
    result = subprocess.run([sys.executable, str(VALIDATOR), "--check"], cwd=ROOT, check=False)
    if result.returncode != 0:
        raise RuntimeError("canonical Agent Skill validation failed")
    integration = HOST_INTEGRATIONS[host]
    mcp_configuration = json.loads((integration / ".mcp.json").read_text(encoding="utf-8"))
    if set(mcp_configuration) != {"mcpServers"} or set(
        mcp_configuration["mcpServers"]
    ) != {"roughcut"}:
        raise RuntimeError(f"{host} MCP configuration contains unexpected fields")
    roughcut_server = mcp_configuration["mcpServers"]["roughcut"]
    if roughcut_server != {"command": "roughcut-mcp", "args": []}:
        raise RuntimeError(f"{host} MCP configuration is invalid")

    if host == "codex":
        manifest = json.loads(
            (integration / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        if set(manifest) != {"name", "version", "description", "skills", "mcpServers"}:
            raise RuntimeError("Codex manifest contains fields outside host metadata")
        if manifest["skills"] != "./skills/" or manifest["mcpServers"] != "./.mcp.json":
            raise RuntimeError("Codex manifest paths are invalid")
    elif not (integration / "README.md").is_file():
        raise RuntimeError("WorkBuddy installation instructions are missing")


def build(
    output: Path,
    host: str,
    mcp_command: str | None,
) -> None:
    if mcp_command is None or (
        not PurePosixPath(mcp_command).is_absolute()
        and not PureWindowsPath(mcp_command).is_absolute()
    ) or "\n" in mcp_command or "\r" in mcp_command:
        raise RuntimeError(
            "Host Package requires an absolute --mcp-command"
        )
    validate_sources(host)
    if output.exists():
        raise RuntimeError(f"output already exists: {output}")
    output.mkdir(parents=True)
    integration = HOST_INTEGRATIONS[host]
    if host == "codex":
        shutil.copytree(integration / ".codex-plugin", output / ".codex-plugin")
    else:
        shutil.copy2(integration / "README.md", output / "README.md")

    mcp_configuration = json.loads((integration / ".mcp.json").read_text(encoding="utf-8"))
    if mcp_command is not None:
        mcp_configuration["mcpServers"]["roughcut"]["command"] = mcp_command
    (output / ".mcp.json").write_text(
        json.dumps(mcp_configuration, indent=2) + "\n", encoding="utf-8"
    )
    generated = subprocess.run(
        [sys.executable, str(VALIDATOR), "--output", str(output)],
        cwd=ROOT,
        check=False,
    )
    checked = subprocess.run(
        [sys.executable, str(VALIDATOR), "--output", str(output), "--check"], cwd=ROOT, check=False
    )
    if generated.returncode != 0 or checked.returncode != 0:
        raise RuntimeError(f"generated {host} workflow drifted from canonical source")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--host", choices=tuple(HOST_INTEGRATIONS))
    parser.add_argument("--mcp-command")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            hosts = (args.host,) if args.host is not None else tuple(HOST_INTEGRATIONS)
            for host in hosts:
                validate_sources(host)
        if args.output is not None:
            build(
                args.output,
                args.host or "codex",
                args.mcp_command,
            )
        if not args.check and args.output is None:
            raise RuntimeError("--output is required unless --check is used")
    except RuntimeError as error:
        print(f"host package build failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
