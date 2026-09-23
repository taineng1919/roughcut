"""Code-only default trial packaging: separation, identity, determinism."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.build_macos_arm64_code_only_trial import (
    CODE_CHECKSUMS_NAME,
    CODE_DIR_NAME,
    INSTALL_GUIDE_NAME,
    KNOWN_ISSUES_NAME,
    MANIFEST_NAME,
    PACKAGING_REPORT_NAME,
    ROOT_CHECKSUMS_NAME,
    ReleaseAssemblyError,
    assemble,
)
from scripts.build_core_release import CORE_VERSION, SOURCE_BUNDLE_NAME, WHEEL_NAME

ROOT = Path(__file__).resolve().parents[2]


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    return result.stdout.strip()


def _build_trial(tmp_path: Path) -> Path:
    out = tmp_path / "trial"
    assemble(ROOT / "core", out, source_commit=_git_head(), require_clean=False)
    return out


def test_code_only_trial_structure_and_manifest(tmp_path: Path) -> None:
    trial = _build_trial(tmp_path)
    assert {path.name for path in trial.iterdir()} == {
        CODE_DIR_NAME,
        INSTALL_GUIDE_NAME,
        MANIFEST_NAME,
        ROOT_CHECKSUMS_NAME,
        KNOWN_ISSUES_NAME,
        PACKAGING_REPORT_NAME,
    }
    code_dir = trial / CODE_DIR_NAME
    assert {path.name for path in code_dir.iterdir()} == {
        WHEEL_NAME,
        SOURCE_BUNDLE_NAME,
        CODE_CHECKSUMS_NAME,
    }
    manifest = json.loads((trial / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["source_commit"] == _git_head()
    assert manifest["code"]["version"] == CORE_VERSION == "0.2.10"
    with zipfile.ZipFile(code_dir / WHEEL_NAME) as wheel:
        metadata = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )
        assert f"Version: {CORE_VERSION}\n" in wheel.read(metadata).decode("utf-8")
    with tarfile.open(code_dir / SOURCE_BUNDLE_NAME, mode="r:gz") as bundle:
        assert f"roughcut-core-{CORE_VERSION}" in {
            Path(member.name).parts[0] for member in bundle.getmembers()
        }
    assert manifest["large_components"] == {
        "included": False,
        "fallback": "offline-v1-only",
    }
    assert manifest["code"]["wheel_sha256"] == _sha(trial / CODE_DIR_NAME / WHEEL_NAME)
    assert manifest["code"]["bundle_sha256"] == _sha(
        trial / CODE_DIR_NAME / SOURCE_BUNDLE_NAME
    )
    assert manifest["separation"]["models"] is False
    assert manifest["separation"]["ffmpeg_binaries"] is False
    assert manifest["separation"]["credentials"] is False


def _sha(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_code_only_trial_has_no_large_payload_or_secrets(tmp_path: Path) -> None:
    trial = _build_trial(tmp_path)
    names = [path.name.lower() for path in trial.rglob("*") if path.is_file()]
    for marker in (
        "funasr",
        "torch",
        "component-cache",
        "runtime.json",
        "large-components",
        ".pt",
        "ffmpeg",
    ):
        assert not any(marker in name for name in names), marker
    manifest = json.loads((trial / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["separation"]["api_keys"] is False
    assert manifest["separation"]["credentials"] is False
    assert manifest["separation"]["user_data"] is False
    assert manifest["separation"]["models"] is False
    texts = [
        path.read_text(encoding="utf-8")
        for path in trial.rglob("*")
        if path.is_file() and path.suffix in {".md", ".txt"}
    ]
    joined = "\n".join(texts)
    # No embedded secret values (manifest attestation keys like "api_keys": false
    # are allowed; they prove absence rather than leaking a value).
    assert "sk-" not in joined
    assert "Bearer " not in joined
    # INSTALL-AGENT/TRIAL docs may mention the private/ directory concept;
    # forbid only absolute user paths, checked separately below.
    # No absolute build-machine paths: staging temp or repo root must not leak.
    assert ".code-only-trial.staging-" not in joined
    assert str(ROOT) not in joined


def test_install_agent_first_action_is_local_probe(tmp_path: Path) -> None:
    trial = _build_trial(tmp_path)
    guide = (trial / INSTALL_GUIDE_NAME).read_text(encoding="utf-8")
    assert "不要立即安装或下载组件" in guide
    assert "第一阶段必须先检查本机已有状态并尽可能 REUSE" in guide
    for step in (
        "verify package SHA",
        "inspect platform/architecture",
        "probe Python",
        "probe existing Roughcut",
        "probe local managed components/cache",
        "probe models",
        "probe Audalign",
        "probe FFmpeg/ffprobe + full capability validation",
        "形成缺失清单",
        "安装/update Code",
        "对缺失组件按多渠道顺序获取",
        "每个 artifact 校验 size/SHA",
        "component PLAN",
        "明确必要 mutation/下载",
        "APPLY",
        "health",
        "diagnostics",
        "ASR smoke",
        "当前正在执行本任务的 Agent 就是唯一接入目标",
        "Core 安装完成不代表当前 Agent 接入完成",
        "当前 Agent 为 Codex",
        "codex mcp add roughcut -- \"$INSTALL_DIR/venv/bin/roughcut-mcp\"",
        "不要对已登记的名称再次运行 `codex mcp add`",
        "$HOME/.agents/skills",
        "重启当前 Codex CLI／IDE 会话",
        "用 `/mcp` 检查 Roughcut 服务",
        "用 `/skills` 检查上述五个 Skills 均可发现",
        "WorkBuddy 5.2.6.0",
        "AGENT_INTEGRATION=INCOMPLETE",
        "不得猜用其他 Agent 的配置步骤",
        "19. continue integration as this task's Agent only",
        "24. report Core and Agent integration statuses separately",
    ):
        assert step in guide
    assert "--populate-cache" in guide
    assert "--populate-tier" in guide
    assert "--populate-component" in guide
    assert "artifact filename is ambiguous; specify --populate-component" in guide
    assert "Scheme B" in guide or "两阶段" in guide
    assert "Large" in guide and "offline" in guide.lower()
    # Python 3.11 missing-path: probe -> REUSE -> bootstrap -> verify exact 3.11 + arm64.
    assert "probe Python 3.11" in guide
    assert "REUSE" in guide
    assert "bootstrap native arm64 Python 3.11" in guide
    assert "verify exact 3.11 + arm64" in guide
    assert "TRIAL_PYTHON" in guide
    # Codex-specific steps and current-Agent-only fallback are executable and explicit.
    assert "roughcut" in guide and "roughcut-basics" in guide
    assert "create-roughcut" in guide and "revise-roughcut" in guide
    assert "render-roughcut" in guide
    assert "ACTIVE_HOST" not in guide
    assert "build_host_package.py --host workbuddy" not in guide
    assert "build_host_package.py --host codex" not in guide
    report = (trial / PACKAGING_REPORT_NAME).read_text(encoding="utf-8")
    assert "7e24fa6" in report and "59da976" in report
    assert "REAL_USER_FRESH_INSTALL_VALIDATION=PENDING" in report
    # Long-task rule: no short-lived background, use operation_id query.
    assert "operation_id" in guide
    assert "nohup" in guide


def test_install_agent_installs_five_canonical_skills_for_current_agent(tmp_path: Path) -> None:
    trial = _build_trial(tmp_path)
    guide = (trial / INSTALL_GUIDE_NAME).read_text(encoding="utf-8")
    assert "安装／更新五个 canonical Skills" in guide
    assert "只有 Core 与 Agent 两部分均完成时才报告整体安装完成" in guide
    for forbidden in (
        "Skills 是 WorkBuddy optional",
        "必须预装 Homebrew",
        "必须预装 Python 3.11 才能开始",
        "只能靠 filename",
        "build 完成就算安装完成",
        "自动安装 WorkBuddy",
        "自动安装 Codex",
    ):
        assert forbidden not in guide


def _guide_shell_block_after(guide: str, marker: str) -> str:
    marker_index = guide.index(marker)
    block_start = guide.index("```sh\n", marker_index) + len("```sh\n")
    block_end = guide.index("\n```", block_start)
    return guide[block_start:block_end]


def _set_shell_assignment(script: str, name: str, value: Path) -> str:
    lines = script.splitlines()
    assignment = f"{name}="
    matches = [index for index, line in enumerate(lines) if line.startswith(assignment)]
    assert len(matches) == 1
    lines[matches[0]] = f"{name}={shlex.quote(str(value))}"
    return "\n".join(lines) + "\n"


def test_codex_guide_sections_run_in_independent_shells_and_clean_source_temps(
    tmp_path: Path,
) -> None:
    trial = _build_trial(tmp_path)
    guide = (trial / INSTALL_GUIDE_NAME).read_text(encoding="utf-8")
    install_dir = tmp_path / "install"
    roughcut = install_dir / "venv" / "bin" / "roughcut"
    roughcut.parent.mkdir(parents=True)
    roughcut.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    roughcut.chmod(0o755)

    trial_python = tmp_path / "trial-python"
    trial_python.write_text(
        "#!/bin/sh\n"
        "test -f \"$1\"\n"
        "test -f \"$(dirname \"$1\")/../agent-skill/skills/roughcut/SKILL.md\"\n",
        encoding="utf-8",
    )
    trial_python.chmod(0o755)

    temporary_root = tmp_path / "shell-tmp"
    temporary_root.mkdir()
    temporary_bin = tmp_path / "shell-bin"
    temporary_bin.mkdir()
    temp_record = tmp_path / "mktemp-paths"
    mktemp = temporary_bin / "mktemp"
    mktemp.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import tempfile\n"
        "path = tempfile.mkdtemp(prefix='roughcut-shell-')\n"
        "with open(os.environ['ROUGHCUT_TMP_RECORD'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(path + '\\n')\n"
        "print(path)\n",
        encoding="utf-8",
    )
    mktemp.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path / "first-home"),
            "PATH": f"{temporary_bin}:{environment.get('PATH', '')}",
            "ROUGHCUT_TMP_RECORD": str(temp_record),
            "TMPDIR": str(temporary_root),
        }
    )
    environment.pop("CORE_ROOT", None)
    environment.pop("CORE_TMP", None)

    core_shell = _guide_shell_block_after(guide, "## 2. 安装/更新 Code")
    core_shell = _set_shell_assignment(core_shell, "PACKAGE_ROOT", trial)
    core_shell = _set_shell_assignment(core_shell, "INSTALL_DIR", install_dir)
    core_shell = _set_shell_assignment(core_shell, "TRIAL_PYTHON", trial_python)
    core_result = subprocess.run(
        ["/bin/sh", "-eu", "-c", core_shell],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert core_result.returncode == 0, core_result.stderr or core_result.stdout
    core_tmp = Path(temp_record.read_text(encoding="utf-8").splitlines()[0])
    assert not core_tmp.exists()

    home = tmp_path / "fresh-shell-home"
    home.mkdir()
    skill_shell = _guide_shell_block_after(
        guide, "安装／更新五个 canonical Skills，并保留每个目录内全部文件"
    )
    skill_shell = _set_shell_assignment(skill_shell, "PACKAGE_ROOT", trial)
    skills_environment = environment.copy()
    skills_environment["HOME"] = str(home)
    skill_result = subprocess.run(
        ["/bin/sh", "-eu", "-c", skill_shell],
        check=False,
        capture_output=True,
        text=True,
        env=skills_environment,
    )
    assert skill_result.returncode == 0, skill_result.stderr or skill_result.stdout
    skill_tmp = Path(temp_record.read_text(encoding="utf-8").splitlines()[1])
    assert skill_tmp != core_tmp
    assert not skill_tmp.exists()

    skills_root = home / ".agents" / "skills"
    expected_skills = {
        "roughcut",
        "roughcut-basics",
        "create-roughcut",
        "revise-roughcut",
        "render-roughcut",
    }
    assert {path.name for path in skills_root.iterdir()} == expected_skills
    with tarfile.open(trial / CODE_DIR_NAME / SOURCE_BUNDLE_NAME, "r:gz") as source:
        for skill in expected_skills:
            prefix = f"roughcut-core-{CORE_VERSION}/agent-skill/skills/{skill}/"
            expected_files = {
                member.name[len(prefix) :]: source.extractfile(member).read()
                for member in source.getmembers()
                if member.isfile() and member.name.startswith(prefix)
            }
            installed = skills_root / skill
            actual_files = {
                path.relative_to(installed).as_posix(): path.read_bytes()
                for path in installed.rglob("*")
                if path.is_file()
            }
            assert actual_files == expected_files


def test_code_only_trial_checksums_cover_payload(tmp_path: Path) -> None:
    trial = _build_trial(tmp_path)
    entries = {}
    for line in (trial / ROOT_CHECKSUMS_NAME).read_text(encoding="utf-8").splitlines():
        digest, _, relative = line.partition("  ")
        entries[relative] = digest
    assert set(entries) == {
        f"{CODE_DIR_NAME}/{WHEEL_NAME}",
        f"{CODE_DIR_NAME}/{SOURCE_BUNDLE_NAME}",
        f"{CODE_DIR_NAME}/{CODE_CHECKSUMS_NAME}",
        INSTALL_GUIDE_NAME,
        MANIFEST_NAME,
        KNOWN_ISSUES_NAME,
        PACKAGING_REPORT_NAME,
    }
    for relative, digest in entries.items():
        assert _sha(trial / relative) == digest


def test_code_only_trial_rejects_dirty_or_mismatched_commit(tmp_path: Path) -> None:
    with pytest.raises(ReleaseAssemblyError):
        assemble(ROOT / "core", tmp_path / "out", source_commit="0" * 40)


def test_code_artifact_determinism_for_same_commit(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    head = _git_head()
    assemble(ROOT / "core", first, source_commit=head, require_clean=False)
    assemble(ROOT / "core", second, source_commit=head, require_clean=False)
    assert _sha(first / CODE_DIR_NAME / WHEEL_NAME) == _sha(
        second / CODE_DIR_NAME / WHEEL_NAME
    )
    assert _sha(first / CODE_DIR_NAME / SOURCE_BUNDLE_NAME) == _sha(
        second / CODE_DIR_NAME / SOURCE_BUNDLE_NAME
    )
