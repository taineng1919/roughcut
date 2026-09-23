from __future__ import annotations

import json
import os
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.build_macos_arm64_trial_distribution as trial_builder
from scripts.build_macos_arm64_trial_distribution import (
    FROZEN_COMMIT,
    INSTALL_AGENT,
    KNOWN_ISSUES,
    load_closure,
    preflight_then_mutate,
    select_trial_python,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "core/src/roughcut/component_catalog"


def runner(
    version: tuple[int, int], machine: str = "arm64", system: str = "Darwin"
):
    def run(argv: list[str], **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "system": system,
                    "sys_version": f"{version[0]}.{version[1]}.0 test",
                    "version": list(version),
                    "machine": machine,
                }
            ),
        )

    return run


def test_fresh_requires_named_native_python311(tmp_path: Path) -> None:
    selected, error = select_trial_python(
        install_root=None,
        candidates=[tmp_path / "python3", tmp_path / "python3.12", tmp_path / "python3.11"],
        runner=runner((3, 11)),
    )
    assert selected == tmp_path / "python3.11"
    assert error is None
    assert select_trial_python(
        install_root=None, candidates=[tmp_path / "python3", tmp_path / "python3.12"], runner=runner((3, 12))
    ) == (None, "PYTHON_311_REQUIRED")
    for version in ((3, 12), (3, 13)):
        mutations: list[Path] = []
        assert preflight_then_mutate(
            install_root=None,
            candidates=[tmp_path / "python3", tmp_path / "python3.11"],
            mutation=mutations.append,
            runner=runner(version),
        ) == (None, "PYTHON_311_REQUIRED")
        assert mutations == []


def test_fresh_skips_nonexistent_and_never_executes_bare_python3(tmp_path: Path) -> None:
    seen: list[str] = []
    def run(argv: list[str], **_: object) -> SimpleNamespace:
        seen.append(argv[0])
        if argv[0].endswith("missing/python3.11"):
            raise FileNotFoundError(argv[0])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "system": "Darwin",
                    "sys_version": "3.11.0 test",
                    "version": [3, 11],
                    "machine": "arm64",
                }
            ),
        )
    selected, error = select_trial_python(
        install_root=None,
        candidates=[tmp_path / "python3", tmp_path / "missing/python3.11", tmp_path / "ok/python3.11"],
        runner=run,
    )
    assert selected == tmp_path / "ok/python3.11" and error is None
    assert str(tmp_path / "python3") not in seen


def test_upgrade_uses_exact_existing_interpreter_and_stops_unsupported(tmp_path: Path) -> None:
    python = tmp_path / "custom/venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    seen: list[list[str]] = []

    def run(argv: list[str], **_: object) -> SimpleNamespace:
        seen.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "system": "Darwin",
                    "sys_version": "3.12.0 test",
                    "version": [3, 12],
                    "machine": "arm64",
                }
            ),
        )

    assert select_trial_python(
        install_root=tmp_path / "custom", candidates=[tmp_path / "python3.11"], runner=run
    ) == (None, "UNSUPPORTED_EXISTING_TRIAL_PYTHON")
    assert seen[0][0] == str(python)
    assert seen[0][1] == "-I"
    assert select_trial_python(
        install_root=tmp_path / "recognized-without-venv", candidates=[tmp_path / "python3.11"], runner=runner((3, 11))
    ) == (None, "UNSUPPORTED_EXISTING_TRIAL_PYTHON")


@pytest.mark.parametrize(
    ("install_root", "system", "machine", "failure"),
    [
        (None, "Linux", "arm64", "PYTHON_311_REQUIRED"),
        (None, "Darwin", "x86_64", "PYTHON_311_REQUIRED"),
        ("existing", "Linux", "arm64", "UNSUPPORTED_EXISTING_TRIAL_PYTHON"),
        ("existing", "Darwin", "x86_64", "UNSUPPORTED_EXISTING_TRIAL_PYTHON"),
    ],
)
def test_platform_preflight_rejects_non_native_macos_before_mutation(
    tmp_path: Path,
    install_root: str | None,
    system: str,
    machine: str,
    failure: str,
) -> None:
    root = tmp_path / install_root if install_root else None
    if root is not None:
        python = root / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
    mutations: list[Path] = []
    assert preflight_then_mutate(
        install_root=root,
        candidates=[tmp_path / "python3.11"],
        mutation=mutations.append,
        runner=runner((3, 11), machine=machine, system=system),
    ) == (None, failure)
    assert mutations == []


def test_closure_is_catalog_derived_and_excludes_bbc_runtime() -> None:
    records, counts = load_closure(CATALOG)
    assert {key: counts[key] for key in ("logical", "unique", "unique_bytes", "model_bytes")} == {
        "logical": 139, "unique": 137, "unique_bytes": 2500953971, "model_bytes": 2218711106,
    }
    profile = counts["profile"]
    assert profile == {
        "id": "macos-arm64-py311", "platform": "macos", "architecture": "arm64", "python_version": "3.11",
        "runtime_artifacts": "python/macos-arm64-py311-artifacts.json", "runtime_lock": "python/macos-arm64-py311.lock",
        "models": "models.json", "audalign_artifacts": "audalign/audalign-macos-arm64-py311-artifacts.json",
        "audalign_lock": "audalign/audalign-macos-arm64-py311.lock", "audalign_license_notice": "audalign/audalign-licenses-macos-arm64-py311.json",
        "versions": {"funasr": "1.3.14", "torch": "2.6.0", "torchaudio": "2.6.0"},
    }
    assert {r["component"] for r in records} >= {
        "python_runtime", "model_asr", "model_vad", "model_punc", "model_spk", "audalign"
    }
    assert "bbc_audio_offset_finder" not in {r["component"] for r in records}
    required = {"filename", "component", "size", "sha256", "source", "license", "cache_destination"}
    assert all(required <= record.keys() for record in records)
    assert all("version" in record or "revision" in record for record in records)


def test_guides_freeze_roots_separation_and_known_issues() -> None:
    assert len(FROZEN_COMMIT) == 40
    assert '$HOME/.roughcut/managed' in INSTALL_AGENT
    assert "download_bytes=0" in INSTALL_AGENT
    assert "UNSUPPORTED_EXISTING_TRIAL_PYTHON" in INSTALL_AGENT
    assert "PYTHON_311_REQUIRED" in INSTALL_AGENT
    assert "cp -R" in INSTALL_AGENT
    assert "打开当前初稿审阅页面。" in KNOWN_ISSUES
    assert "读取当前初稿实际时长；如果低于目标继续调整，不要确认。" in KNOWN_ISSUES


def test_guide_keeps_core_upgrade_independent_and_uses_exact_interpreter() -> None:
    upgrade, component = INSTALL_AGENT.split(
        "## FRESH / COMPONENT-INSTALL — Large entry", 1
    )
    upgrade_python_commands = [
        line
        for line in upgrade.splitlines()
        if line.startswith('"$') and (" -I " in line or "scripts/" in line or ' - "$' in line)
    ]
    assert trial_builder.LARGE_ARCHIVE not in upgrade
    assert "$PY311" not in upgrade
    assert "--component-cache" not in upgrade
    assert "--apply-components" not in upgrade
    assert "stage and verify FFmpeg" not in upgrade
    assert 'TRIAL_PYTHON="$INSTALL_ROOT/venv/bin/python"' in upgrade
    assert upgrade_python_commands
    assert all(line.startswith('"$TRIAL_PYTHON"') for line in upgrade_python_commands)
    assert trial_builder.LARGE_ARCHIVE in component
    assert "--component-cache" in component and "--apply-components" in component
    assert "$PY311" not in component
    assert "Healthy bindings are `REUSE`" in component
    python_commands = [
        line
        for line in component.splitlines()
        if line.startswith('"$') and "scripts/" in line
    ]
    assert python_commands
    assert all(line.startswith('"$TRIAL_PYTHON"') for line in python_commands)
    assert "platform.system()==\"Darwin\"" in upgrade
    assert "sys_version" in upgrade and "sys_version" in component


def test_component_entry_preflight_precedes_every_mutating_phase() -> None:
    component = INSTALL_AGENT.split(
        "## FRESH / COMPONENT-INSTALL — Large entry", 1
    )[1]
    hash_verification = component.index("First perform the read-only archive hash verification")
    preflight = component.index("Next complete preflight before any mutation")
    fresh_probe = component.index('"$CANDIDATE" -I -c')
    existing_probe = component.index('"$TRIAL_PYTHON" -I -c')
    mutation_boundary = component.index("Only after the applicable preflight succeeds")
    assert hash_verification < preflight < fresh_probe < existing_probe < mutation_boundary
    for mutation in (
        "safely unpack each required archive",
        "create staging",
        "prepare FFmpeg",
        "bootstrap Core",
        "run PLAN/APPLY",
    ):
        assert component.index(mutation, mutation_boundary) >= mutation_boundary


@pytest.mark.parametrize(
    ("install_root", "system", "failure"),
    [
        (None, "Linux", "PYTHON_311_REQUIRED"),
        ("existing", "Linux", "UNSUPPORTED_EXISTING_TRIAL_PYTHON"),
    ],
)
def test_component_preflight_failure_never_reaches_unpack_mutation(
    tmp_path: Path, install_root: str | None, system: str, failure: str
) -> None:
    root = tmp_path / install_root if install_root else None
    if root is not None:
        python = root / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
    unpack_mutations: list[Path] = []
    assert preflight_then_mutate(
        install_root=root,
        candidates=[tmp_path / "python3.11"],
        mutation=unpack_mutations.append,
        runner=runner((3, 11), system=system),
    ) == (None, failure)
    assert unpack_mutations == []


def test_frozen_core_assembly_uses_the_frozen_release_identity(tmp_path: Path) -> None:
    """The frozen `0.2.7` unit must be assembled by the frozen checkout's builder.

    `build_core_release` in the current worktree assembles whatever Core identity
    this branch carries; binding the frozen archives to it would make the frozen
    build fail the wheel metadata check (or silently re-label the archives).  The
    environment-gated built-distribution test cannot catch that, so this asserts
    the assembler identity directly.
    """

    checkout = tmp_path / "frozen-source"
    try:
        trial_builder.export_frozen_source(checkout)
    except trial_builder.TrialDistributionError as error:
        if "FROZEN_CORE_COMMIT_UNAVAILABLE" not in str(error):
            raise
        pytest.skip("the frozen commit is unavailable in this checkout")

    assembler = trial_builder.frozen_release_assembler(checkout)

    assert Path(assembler.__file__).resolve() == (
        checkout / "scripts" / "build_core_release.py"
    ).resolve()
    assert assembler.CORE_VERSION == "0.2.7"
    assert str(checkout) not in sys.path


def test_built_distribution_manifest_report_and_separation() -> None:
    value = os.environ.get("ROUGHCUT_TRIAL_DISTRIBUTION")
    if not value:
        pytest.skip("set ROUGHCUT_TRIAL_DISTRIBUTION to validate built archives")
    root = Path(value)
    assert (root / "INSTALL-AGENT.md").read_text() == INSTALL_AGENT
    top = json.loads((root / "TRIAL-MANIFEST.json").read_text())
    assert top["core"]["sha256"] == trial_builder.EXPECTED_CORE_ARCHIVE_SHA
    assert top["large_components"]["sha256"] == (
        "1221397b167f959cb02580efe8bc84c176d24a2c0f766532885e93623a5f2f20"
    )
    assert top["large_components"]["version"] == "macos-arm64-py311-v1"
    report = (root / "PACKAGING-REPORT.md").read_text()
    assert "must be filled" not in report
    assert "Deterministic double build: PASS" in report
    with tarfile.open(root / top["large_components"]["archive"], "r:gz") as archive:
        names = archive.getnames()
        manifest_name = next(name for name in names if name.endswith("/manifest.json"))
        stream = archive.extractfile(manifest_name)
        assert stream is not None
        manifest = json.load(stream)
    required = {"filename", "component", "size", "sha256", "source", "license", "cache_destination"}
    assert all(required <= record.keys() for record in manifest["artifacts"])
    assert all("version" in record or "revision" in record for record in manifest["artifacts"])
    joined = "\n".join(names)
    assert "runtime.json" not in joined and "component-manifest.json" not in joined
    assert "roughcut-0.2.7-py3-none-any.whl" not in joined


def test_metadata_refresh_regenerates_guide_without_rebuilding_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference, output = tmp_path / "reference", tmp_path / "output"
    reference.mkdir()
    for name, data in ((trial_builder.CORE_ARCHIVE, b"core"), (trial_builder.LARGE_ARCHIVE, b"large")):
        (reference / name).write_bytes(data)
    core_sha = trial_builder.sha256(reference / trial_builder.CORE_ARCHIVE)
    monkeypatch.setattr(trial_builder, "EXPECTED_CORE_ARCHIVE_SHA", core_sha)
    trial_builder.write_json(reference / "TRIAL-MANIFEST.json", {
        "core": {"size": 4, "sha256": core_sha},
        "large_components": {"size": 5, "sha256": trial_builder.sha256(reference / trial_builder.LARGE_ARCHIVE)},
    })
    (reference / "PACKAGING-REPORT.md").write_text("Deterministic double build: PASS\n")
    (reference / "KNOWN-ISSUES.md").write_text("known\n")
    trial_builder.refresh_metadata(reference, output)
    assert (output / trial_builder.CORE_ARCHIVE).read_bytes() == b"core"
    assert (output / trial_builder.LARGE_ARCHIVE).read_bytes() == b"large"
    assert "UNSAFE_TRIAL_ARCHIVE" in (output / "INSTALL-AGENT.md").read_text()
    assert (output / "SHA256SUMS").is_file()
