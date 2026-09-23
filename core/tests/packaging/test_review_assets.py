from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, build_opener

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import create_edit_brief, read_agent_context
from roughcut.application.projects import create_project
from roughcut.application.proposals import create_edit_proposal
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance, TranscriptSegment

ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT / "core" / "src" / "roughcut" / "review" / "static"
REVIEW_UI = ROOT / "review-ui"
NPM = "npm.cmd" if os.name == "nt" else "npm"
AUDALIGN_CATALOG_PATHS = (
    "core/src/roughcut/component_catalog/audalign/audalign-licenses-macos-arm64-py311.json",
    "core/src/roughcut/component_catalog/audalign/audalign-licenses-windows-x64-py311.json",
    "core/src/roughcut/component_catalog/audalign/audalign-macos-arm64-py311-artifacts.json",
    "core/src/roughcut/component_catalog/audalign/audalign-macos-arm64-py311.lock",
    "core/src/roughcut/component_catalog/audalign/audalign-windows-x64-py311-artifacts.json",
    "core/src/roughcut/component_catalog/audalign/audalign-windows-x64-py311.lock",
)
BBC_CATALOG_PATHS = (
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-licenses-macos-arm64-py311.json",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-licenses-windows-x64-py311.json",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-macos-arm64-py311-artifacts.json",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-macos-arm64-py311.lock",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-windows-x64-py311-artifacts.json",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-windows-x64-py311.lock",
)


@pytest.fixture(scope="session")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    output = tmp_path_factory.mktemp("review-distributions")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            str(ROOT / "core"),
            "--outdir",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return next(output.glob("*.whl")), next(output.glob("*.tar.gz"))


def _git_index_bytes(path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f":{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout


def _static_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(path for path in root.rglob("*") if path.is_file())
    }


def _project(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "installed review project"
    media = tmp_path / "browser.mp4"
    media.write_bytes(b"fixture media")
    project = create_project(root, "Installed review")
    source_id = "src_package"
    transcript_id = "tr_package"
    source = SourceAsset(
        source_id,
        "video",
        media.name,
        ImportMode.LINKED,
        {"absolute_path": str(media.resolve())},
        SourceFingerprint(media.stat().st_size, media.stat().st_mtime_ns, "fixture"),
        MediaProbe(1_200_000, 0, 0, "h264", 320, 180, {"numerator": 25, "denominator": 1}, False, "aac", 48_000, 0),
    )
    transcript = TimedTranscript(
        1,
        transcript_id,
        source_id,
        None,
        TranscriptProvenance("fixture", "1", {}, {}, "raw-asr/fixture.json", "a", "b", 0),
        "zh-CN",
        tuple(
            TranscriptSegment(
                f"seg_{index:06d}",
                (index - 1) * 120_000,
                index * 120_000,
                f"第{index}句。",
                None,
                None,
                None,
                None,
                (),
                "unmarked",
            )
            for index in range(1, 11)
        ),
    )
    write_new_json(root / "transcripts" / source_id / f"{transcript_id}.json", transcript.to_dict())
    imported = replace(
        project,
        revision=1,
        sources=(source,),
        active_transcript_versions={source_id: transcript_id},
    )
    ProjectStore(root).save(imported, expected_revision=0)
    brief = create_edit_brief(
        root,
        theme="package",
        target_duration_ticks=1_200_000,
        focus=["all"],
        allow_reorder=True,
        expected_revision=1,
    )
    context = read_agent_context(
        root,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=2,
        offset=0,
        limit=10,
    )
    proposal = create_edit_proposal(
        root,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        clips=[
            {
                "clip_id": f"clip_{index:06d}",
                "source_id": source_id,
                "transcript_version_id": transcript_id,
                "segment_id": f"seg_{index:06d}",
                "source_in_ticks": (index - 1) * 120_000,
                "source_out_ticks": index * 120_000,
                "reason": "fixture",
                "display_text": f"第{index}句。",
            }
            for index in range(1, 11)
        ],
        total_duration_ticks=1_200_000,
        expected_revision=2,
    )
    return root, proposal.proposal.proposal_id


def test_review_source_and_built_assets_are_small_and_framework_free() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    scripts = list((STATIC / "assets").glob("*.js"))
    styles = list((STATIC / "assets").glob("*.css"))

    assert len(scripts) == 1
    assert len(styles) == 1
    assert f"/assets/{scripts[0].name}" in index
    assert f"/assets/{styles[0].name}" in index
    assert "react" not in index.lower()
    assert "vue" not in index.lower()
    assert "svelte" not in index.lower()
    assert 'id="workflow-review"' in index
    assert 'id="draft-editor-shell"' in index
    assert 'id="draft-document"' in index
    assert 'id="draft-source-document"' in index
    assert 'id="draft-source-select"' in index
    assert 'id="draft-player"' in index
    assert 'id="draft-move-cancel"' not in index
    assert 'id="draft-move-target-summary"' not in index
    assert 'id="draft-structure-overview"' not in index
    assert 'id="draft-structure-list"' not in index
    assert "结构概览" not in index
    assert 'id="draft-section-split"' in index
    assert "从这里开始新章节" in index
    assert "上移" not in index
    assert "下移" not in index
    assert "合并前章" not in index
    assert "合并后章" not in index
    assert "删除章节" not in index
    assert "确认初稿并生成粗剪预览" in index
    assert 'id="draft-agent-copy"' in index
    assert 'id="draft-agent-copy-status" aria-live="polite"' in index
    assert "复制给 Agent 调整" in index
    assert "不会自动采用、正式渲染或导出" in index
    assert 'id="workflow-stage-overview"' not in index
    assert 'id="workflow-stage-brief"' not in index
    assert 'id="workflow-propose"' not in index
    assert "Content Draft 主稿本" not in index
    assert "保存新 candidate" not in index
    assert "生成可剪 Proposal" not in index
    assert '<section id="artifact-review" hidden></section>' in index
    assert '<template id="roughcut-review-template">' in index
    assert "粗剪预览" in index
    assert 'id="roughcut-player"' in index
    assert 'id="roughcut-manuscript"' in index
    assert 'id="roughcut-transport-play"' in index
    assert 'id="roughcut-transport-seek"' in index
    assert 'id="roughcut-sequence"' not in index
    assert 'id="roughcut-undo"' not in index
    assert 'id="roughcut-redo"' not in index
    assert "采用这个粗剪版本" in index
    assert "返回初稿调整" in index
    assert "确认渲染" not in index
    assert "正式渲染或导出" in index
    assert "确认当前剪辑方案" not in index
    assert "已确认剪辑版本历史" not in index
    assert "转录稿版本" not in index
    assert "技术详情" not in index
    assert "Render" not in index
    assert '<video id="roughcut-player" preload="metadata"></video>' in index
    assert "调整片段" not in index
    assert "已删除片段" not in index
    bundled_javascript = "\n".join(path.read_text(encoding="utf-8") for path in scripts)
    assert "window_endpoint" in bundled_javascript
    assert "正式导出当前已采用的粗剪版本" in bundled_javascript
    assert "/api/workflow/readable-transcript" not in bundled_javascript
    # The framework-free Review bundle remains bounded after direct-drop,
    # schema-2 section editing, punctuation input, and the bounded interaction
    # trace are included in the single generated chunk.
    assert max(path.stat().st_size for path in scripts) < 115_000
    assert sum(path.stat().st_size for path in STATIC.rglob("*") if path.is_file()) < 170_000


def test_canonical_review_build_matches_shipped_static_bytes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "review-static"
    environment = dict(os.environ)
    environment["npm_config_offline"] = "true"
    result = subprocess.run(
        [
            NPM,
            "--prefix",
            str(REVIEW_UI),
            "run",
            "build",
            "--",
            "--outDir",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert _static_snapshot(output) == _static_snapshot(STATIC)


def test_initial_draft_source_keeps_editor_states_playback_and_pages_isolated() -> None:
    index = (REVIEW_UI / "index.html").read_text(encoding="utf-8")
    workflow = (REVIEW_UI / "src" / "workflow.ts").read_text(encoding="utf-8")
    correspondence = (REVIEW_UI / "src" / "draft-correspondence.ts").read_text(
        encoding="utf-8"
    )
    model = (REVIEW_UI / "src" / "draft-editor-model.ts").read_text(
        encoding="utf-8"
    )
    styles = (REVIEW_UI / "src" / "style.css").read_text(encoding="utf-8")

    assert index.count(">初稿调整<") == 1
    assert 'id="draft-player-toggle"' in index
    assert 'id="draft-player-expanded"' in index
    assert 'id="draft-correspondence-nav"' in index
    assert '<section id="artifact-review" hidden></section>' in index
    assert '<template id="roughcut-review-template">' in index
    assert "sourceMarker(" not in workflow
    assert "linkSourceRun(" not in workflow
    assert "DraftPlaybackQueue" in workflow
    assert "selection.correspondence_groups" in correspondence
    assert "draftSelection" in model
    assert "sourceSelection" in model
    assert "min-height: 214px" not in styles
    assert '.draft-player[data-expanded="false"]' in styles
    assert ".draft-source-marker" not in styles
    assert "height: clamp(" not in styles
    assert "760px" not in styles
    assert "height: 100dvh" in styles
    assert "min-height: 532px" not in styles
    assert "overflow: hidden; flex-direction: column" in styles
    assert "draft-agent-copy" in workflow
    assert "visibleSelectionQuote" in workflow
    assert "degradedPromptOperation" in workflow
    assert "rejectUnresolvedSelection" in workflow
    assert "selection?.response.resolution.canonical_text" not in workflow
    assert "draft-caret-hit" not in workflow
    assert "draft-caret-hit" not in styles
    assert "DraftPointerGesture" in workflow
    assert "moveTargetMode" not in workflow
    assert "draftStructureEntries" not in workflow
    assert "draft-structure-overview" not in workflow
    assert "draft-structure-list" not in workflow
    assert "/api/workflow/draft-caret-resolve" in workflow
    assert "移动到这里" not in workflow
    assert "移动所选" not in workflow
    assert "加入初稿" not in workflow
    assert "endpointFromRenderedFragments" in workflow
    assert "draft-section-drag-handle" in workflow
    assert "draft-section-menu" in workflow
    assert "并入上一章" in workflow
    assert "删除本章及内容" in workflow
    assert "上移" not in workflow
    assert "下移" not in workflow
    assert "合并前章" not in workflow
    assert "合并后章" not in workflow
    assert "删除章节" not in workflow
    assert "caretPositionFromPoint" in workflow
    assert "getClientRects()[0]" not in workflow
    assert "继续修改" in workflow
    assert "保存变化会创建不可变 child" in workflow
    assert "pointer-events: none" in styles
    assert "user-select: none" in styles
    assert ".draft-drop-indicator { position: absolute;" in styles
    assert ".draft-drop-indicator::after" not in styles


def test_wheel_and_sdist_include_built_review_assets(
    built_distributions: tuple[Path, Path],
) -> None:
    wheel, sdist = built_distributions
    catalog_paths = (*AUDALIGN_CATALOG_PATHS, *BBC_CATALOG_PATHS)
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        wheel_payloads = {
            path: archive.read(path.removeprefix("core/src/"))
            for path in catalog_paths
        }
    with tarfile.open(sdist) as archive:
        sdist_names = archive.getnames()
        sdist_payloads = {
            path: archive.extractfile(
                next(
                    name
                    for name in sdist_names
                    if name.endswith(path.removeprefix("core/src/"))
                )
            ).read()
            for path in catalog_paths
        }

    assert "roughcut/review/static/index.html" in wheel_names
    assert any(name.startswith("roughcut/review/static/assets/") for name in wheel_names)
    assert "roughcut/component_catalog/release-catalog.json" in wheel_names
    assert "roughcut/component_catalog/models.json" in wheel_names
    assert "roughcut/component_catalog/python/macos-arm64-py311.lock" in wheel_names
    assert "roughcut/component_catalog/python/windows-x64-py311.lock" in wheel_names
    assert all(path.removeprefix("core/src/") in wheel_names for path in catalog_paths)
    assert any(name.endswith("/roughcut/review/static/index.html") for name in sdist_names)
    assert any("/roughcut/review/static/assets/" in name for name in sdist_names)
    assert any(name.endswith("/roughcut/component_catalog/models.json") for name in sdist_names)
    assert any(
        name.endswith("/roughcut/component_catalog/python/macos-arm64-py311.lock")
        for name in sdist_names
    )
    assert all(
        any(name.endswith(path.removeprefix("core/src/")) for name in sdist_names)
        for path in catalog_paths
    )
    for path in catalog_paths:
        source_bytes = _git_index_bytes(path)
        assert wheel_payloads[path] == source_bytes
        assert sdist_payloads[path] == source_bytes


def test_installed_wheel_starts_and_serves_review_with_node_hidden(
    tmp_path: Path, built_distributions: tuple[Path, Path]
) -> None:
    wheel, _sdist = built_distributions
    environment = tmp_path / "isolated-runtime"
    site_packages = environment / "site-packages"
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(site_packages),
            str(wheel),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    assert list(site_packages.glob("roughcut-*.dist-info"))
    project, proposal_id = _project(tmp_path)
    runtime_env = dict(os.environ)
    runtime_bin = environment / "runtime-bin"
    runtime_bin.mkdir()
    runtime_env["PATH"] = str(runtime_bin)
    runtime_env.pop("PYTHONPATH", None)
    runtime_env.pop("PYTHONHOME", None)
    assert shutil.which("node", path=runtime_env["PATH"]) is None
    assert shutil.which("npm", path=runtime_env["PATH"]) is None

    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import runpy,sys;"
                f"sys.path.insert(0,{str(site_packages)!r});"
                "runpy.run_module('roughcut.cli',run_name='__main__')"
            ),
            "review",
            str(project),
            "--proposal-id",
            proposal_id,
            "--json",
        ],
        cwd=tmp_path,
        env=runtime_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        startup = json.loads(process.stdout.readline())
        url = startup["review"]["url"]
        opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        index = opener.open(url, timeout=3).read().decode("utf-8")
        asset_path = re.search(r'src="(/assets/[^"]+\.js)"', index)
        assert asset_path is not None
        origin = re.match(r"http://127\.0\.0\.1:\d+", url)
        assert origin is not None
        asset = opener.open(origin.group(0) + asset_path.group(1), timeout=3).read()
        assert len(asset) > 100
    finally:
        process.terminate()
        process.wait(timeout=5)
    parsed_port = int(re.search(r":(\d+)/", url).group(1))  # type: ignore[union-attr]
    deadline = time.monotonic() + 3
    while True:
        with socket.socket() as probe:
            probe.settimeout(0.3)
            listener_closed = probe.connect_ex(("127.0.0.1", parsed_port)) != 0
        if listener_closed or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    assert listener_closed
    with pytest.raises((URLError, TimeoutError)):
        build_opener().open(url, timeout=0.3)
