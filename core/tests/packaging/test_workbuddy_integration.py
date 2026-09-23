from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BUILD_HOST_PACKAGE = ROOT / "scripts" / "build_host_package.py"
WORKBUDDY_INTEGRATION = ROOT / "host-integrations" / "workbuddy"
CANONICAL_SKILLS = {
    name: ROOT / "agent-skill" / "skills" / name / "SKILL.md"
    for name in (
        "roughcut",
        "roughcut-basics",
        "create-roughcut",
        "revise-roughcut",
        "render-roughcut",
    )
}


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)


def test_workbuddy_source_is_only_host_metadata() -> None:
    mcp = json.loads((WORKBUDDY_INTEGRATION / ".mcp.json").read_text(encoding="utf-8"))

    assert mcp == {
        "mcpServers": {
            "roughcut": {
                "command": "roughcut-mcp",
                "args": [],
            }
        }
    }
    assert not list(WORKBUDDY_INTEGRATION.rglob("SKILL.md"))
    assert (WORKBUDDY_INTEGRATION / "README.md").is_file()


def test_build_workbuddy_package_uses_canonical_skills(tmp_path: Path) -> None:
    output = tmp_path / "roughcut-workbuddy"
    mcp_command = r"C:\Developer\transcript-roughcut\.venv\Scripts\roughcut-mcp.exe"

    result = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--host",
        "workbuddy",
        "--output",
        str(output),
        "--mcp-command",
        mcp_command,
    )

    assert result.returncode == 0, result.stderr
    assert not (output / ".codex-plugin").exists()
    assert (output / "README.md").is_file()
    mcp = json.loads((output / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["roughcut"] == {"command": mcp_command, "args": []}
    for name, canonical_skill in CANONICAL_SKILLS.items():
        generated_skill = output / "skills" / name / "SKILL.md"
        assert generated_skill.read_text(encoding="utf-8") == canonical_skill.read_text(
            encoding="utf-8"
        )
        for reference in re.findall(
            r"`([^`]+\.md)`", generated_skill.read_text(encoding="utf-8")
        ):
            assert (generated_skill.parent / reference).is_file()


def test_build_workbuddy_package_never_contains_component_environment(
    tmp_path: Path,
) -> None:
    output = tmp_path / "roughcut-workbuddy"
    mcp_command = "/Users/test/.roughcut/venv/bin/roughcut-mcp"

    result = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--host",
        "workbuddy",
        "--output",
        str(output),
        "--mcp-command",
        mcp_command,
    )

    assert result.returncode == 0, result.stderr
    mcp = json.loads((output / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["roughcut"] == {
        "command": mcp_command,
        "args": [],
    }


def test_build_workbuddy_package_rejects_removed_component_env_arguments(
    tmp_path: Path,
) -> None:
    output = tmp_path / "roughcut-workbuddy"

    result = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--host",
        "workbuddy",
        "--output",
        str(output),
        "--funasr-python",
        "/Users/test/funasr/bin/python",
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --funasr-python" in result.stderr
    assert not output.exists()


def test_workbuddy_source_and_generated_workflows_pass_drift_checks(tmp_path: Path) -> None:
    output = tmp_path / "roughcut-workbuddy"
    built = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--host",
        "workbuddy",
        "--output",
        str(output),
        "--mcp-command",
        "/tmp/roughcut-mcp",
    )
    source_check = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--host",
        "workbuddy",
        "--check",
    )
    generated_check = run(
        sys.executable,
        str(ROOT / "scripts" / "validate_agent_skills.py"),
        "--output",
        str(output),
        "--check",
    )

    assert built.returncode == 0, built.stderr
    assert source_check.returncode == 0, source_check.stderr
    assert generated_check.returncode == 0, generated_check.stderr


def test_workbuddy_host_package_expects_mcp_and_five_skills(tmp_path: Path) -> None:
    output = tmp_path / "roughcut-workbuddy"
    result = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--host",
        "workbuddy",
        "--output",
        str(output),
        "--mcp-command",
        "/tmp/roughcut-mcp",
    )
    assert result.returncode == 0, result.stderr
    mcp = json.loads((output / ".mcp.json").read_text(encoding="utf-8"))
    assert "roughcut" in mcp["mcpServers"]
    for name in (
        "roughcut",
        "roughcut-basics",
        "create-roughcut",
        "revise-roughcut",
        "render-roughcut",
    ):
        assert (output / "skills" / name / "SKILL.md").is_file()
    readme = (WORKBUDDY_INTEGRATION / "README.md").read_text(encoding="utf-8")
    generated_readme = (output / "README.md").read_text(encoding="utf-8")
    # WorkBuddy source README carries the same integration contract (generated README
    # is a copy of the source).
    assert generated_readme == readme
    # Skills are an automatic standard item, not an optional user choice.
    assert "5" in readme or "five" in readme.lower()
    for forbidden in (
        "Skills 是 WorkBuddy optional",
        "必须预装 Homebrew",
    ):
        assert forbidden not in readme
        assert forbidden not in generated_readme
    # Safe-delete warning must precede mutating CLI usage.
    safe_index = readme.find("safe-delete")
    assert safe_index != -1
    assert safe_index < readme.find("Merge the generated")
