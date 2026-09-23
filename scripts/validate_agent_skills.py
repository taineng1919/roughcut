"""Generate and validate host-neutral Agent Skill workflow sources."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILL_NAMES = (
    "roughcut",
    "roughcut-basics",
    "create-roughcut",
    "revise-roughcut",
    "render-roughcut",
)
CANONICAL_SKILLS = {
    name: ROOT / "agent-skill" / "skills" / name / "SKILL.md"
    for name in CANONICAL_SKILL_NAMES
}
CANONICAL_CONTRACT = ROOT / "docs" / "agent-tool-contract.md"
SKILL_CONTRACTS = {
    name: path.parent / "references" / "tool-contract.md"
    for name, path in CANONICAL_SKILLS.items()
}
SKILL_SOURCE_DIRECTORIES = (ROOT / "agent-skill", ROOT / "host-integrations")
FORBIDDEN_MARKERS = (".codex-plugin", "${claude_", "claude code", "workbuddy")
LOCAL_MARKDOWN_REFERENCE = re.compile(r"`([^`]+\.md)`")


def fail(message: str) -> None:
    print(f"agent skill validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_local_references(content: str, skill_directory: Path) -> None:
    for reference in LOCAL_MARKDOWN_REFERENCE.findall(content):
        if not (skill_directory / reference).is_file():
            fail(f"Skill reference does not exist: {reference}")


def validate_canonical_sources() -> dict[str, str]:
    contents: dict[str, str] = {}
    for name, skill_path in CANONICAL_SKILLS.items():
        content = skill_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if len(lines) < 5 or lines[0] != "---" or lines[3] != "---":
            fail(f"canonical Skill {name} must contain a three-line YAML frontmatter block")
        if lines[1] != f"name: {name}" or not lines[2].startswith("description: "):
            fail(f"canonical Skill {name} frontmatter may only contain name and description")
        lowered = content.lower()
        if any(marker in lowered for marker in FORBIDDEN_MARKERS):
            fail(f"canonical Skill {name} contains a host-specific marker")
        validate_local_references(content, skill_path.parent)
        contents[name] = content
    return contents


def validate_contract_references() -> None:
    contract = CANONICAL_CONTRACT.read_text(encoding="utf-8")
    for name, skill_contract in SKILL_CONTRACTS.items():
        if skill_contract.read_text(encoding="utf-8") != contract:
            fail(f"{name} tool contract has drifted from docs/agent-tool-contract.md")


def validate_no_handwritten_copies() -> None:
    skill_files = sorted(
        path for source_directory in SKILL_SOURCE_DIRECTORIES for path in source_directory.rglob("SKILL.md")
    )
    unexpected = [
        path.relative_to(ROOT)
        for path in skill_files
        if path not in CANONICAL_SKILLS.values()
    ]
    if unexpected:
        fail(f"unexpected handwritten workflow copies: {', '.join(map(str, unexpected))}")


def generated_skill_path(output: Path, name: str) -> Path:
    return output / "skills" / name / "SKILL.md"


def generated_contract_path(output: Path, name: str) -> Path:
    return generated_skill_path(output, name).parent / "references" / "tool-contract.md"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    contents = validate_canonical_sources()
    validate_contract_references()
    validate_no_handwritten_copies()

    if args.output is not None:
        for name, content in contents.items():
            destination = generated_skill_path(args.output, name)
            contract_destination = generated_contract_path(args.output, name)
            if args.check:
                if not destination.is_file() or destination.read_text(encoding="utf-8") != content:
                    fail(f"generated {name} workflow has drifted from the canonical source")
                if contract_destination.read_text(encoding="utf-8") != SKILL_CONTRACTS[
                    name
                ].read_text(encoding="utf-8"):
                    fail(f"generated {name} contract has drifted from the canonical source")
                validate_local_references(
                    destination.read_text(encoding="utf-8"), destination.parent
                )
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                contract_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(SKILL_CONTRACTS[name], contract_destination)
    print("agent skill validation passed")


if __name__ == "__main__":
    main()
