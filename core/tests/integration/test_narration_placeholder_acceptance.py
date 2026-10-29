"""Acceptance rebuild for the unrecorded-narration placeholder short sample.

This module is both the repeatable sample builder and its automated checks:

    PYTHONPATH=core/src:. core/.venv/bin/python -m pytest \
        core/tests/integration/test_narration_placeholder_acceptance.py -q

It rebuilds a deterministic 14-second, 25 fps project (two cameras, two
persons, two chapters, two unrecorded narrations with one automatic estimate
and one manual override) with real FFmpeg media, runs the full confirmed-draft
-> Proposal -> adoption -> preview -> MP4 render -> multicam parallel render ->
FCPXML/FCP7 XML export chain, and verifies the placeholder media, silence,
frame quotas, XML locators, receipts and fail-closed tamper handling.

Generated media and XML stay under ``.acceptance/narration-placeholder-sample``
(override with ``ROUGHCUT_ACCEPTANCE_DIR``) and are never committed.
"""

from __future__ import annotations

import hashlib
import http.client
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.multicam_parallel import (
    prepare_multicam_parallel_render,
    start_multicam_parallel_render,
)
from roughcut.application.narration_media import (
    ensure_narration_placeholder_media,
    placeholder_media_path,
    read_placeholder_media,
)
from roughcut.application.nle_handoff import approve_nle_export
from roughcut.application.preview import load_review_snapshot
from roughcut.application.projects import create_project
from roughcut.application.renders import prepare_render_plan, render_roughcut
from roughcut.application.sources import add_source
from roughcut.application.workflows import (
    workflow_action,
    workflow_start,
    workflow_status,
)
from roughcut.domain.alignment import (
    MulticamAlignmentArtifact,
)
from roughcut.domain.edit import NarrationPlaceholderClip
from roughcut.domain.nle_handoff import NleHandoffError, placeholder_companion_directory
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import ImportMode, ProjectError
from roughcut.domain.render import OutputSettings, ToolResolution
from roughcut.domain.time import RationalRate
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.review.server import start_review_server

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = REPO_ROOT / ".acceptance" / "narration-placeholder-sample"
TICKS = 120_000
RUN_ID = "wfr_sample"
FPS = RationalRate(25, 1)
MAIN_SEGMENT_COLORS = ("red", "green", "blue", "yellow", "magenta", "cyan")
AUX_SEGMENT_COLORS = ("0x202020", "0x303030", "0x404040", "0x505050", "0x606060", "0x707070")
SEGMENT_FREQUENCIES = (440, 550, 660, 770, 880, 990)
SEGMENT_SECONDS = 16


def _tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.fail("acceptance sample requires ffmpeg and ffprobe on PATH")
    return ffmpeg, ffprobe


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, check=False, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result


def _make_marked_source(path: Path, colors: tuple[str, ...]) -> None:
    ffmpeg, _ffprobe = _tools()
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    for color, frequency in zip(colors, SEGMENT_FREQUENCIES, strict=True):
        command.extend(
            ["-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=25:d={SEGMENT_SECONDS}"]
        )
        command.extend(
            ["-f", "lavfi", "-i", f"sine=f={frequency}:sample_rate=48000:d={SEGMENT_SECONDS}"]
        )
    inputs = "".join(f"[{index * 2}:v][{index * 2 + 1}:a]" for index in range(len(colors)))
    command.extend(
        [
            "-filter_complex",
            f"{inputs}concat=n={len(colors)}:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )
    _run(command)


def _fine_units(text: str, start: int) -> tuple[Any, ...]:
    from roughcut.domain.transcript import FineUnit

    return tuple(
        FineUnit(
            "character",
            character,
            start + index * (TICKS // 10),
            start + (index + 1) * (TICKS // 10),
            None,
        )
        for index, character in enumerate(text)
    )


def _segment(
    segment_id: str,
    start: int,
    end: int,
    text: str,
    speaker: str,
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start_ticks=start,
        end_ticks=end,
        original_text=text,
        corrected_text=None,
        local_speaker_id=speaker,
        person_id=None,
        confidence=None,
        fine_units=_fine_units(text, start),
        editorial_mark="unmarked",
    )


def _transcript(
    source_id: str,
    transcript_id: str,
    segments: tuple[TranscriptSegment, ...],
) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "acceptance-fixture",
            "1",
            {},
            {},
            f"raw-asr/{source_id}/acceptance.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=segments,
    )


def _multicam_setup(main_source_id: str, aux_source_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": [main_source_id]},
        "auxiliary_cameras": [
            {"camera_id": "aux_1", "ordered_source_ids": [aux_source_id]}
        ],
        "source_pairs": [
            {"main_source_id": main_source_id, "auxiliary_source_id": aux_source_id}
        ],
    }


def _http(
    review: Any,
    method: str,
    path: str,
    *,
    body: dict[str, object] | bytes | None = None,
    write: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    headers = {"X-Roughcut-Token": review.token}
    if write:
        headers.update(
            {
                "Origin": f"http://127.0.0.1:{review.port}",
                "Content-Type": "application/json",
            }
        )
    if extra_headers is not None:
        headers.update(extra_headers)
    connection = http.client.HTTPConnection("127.0.0.1", review.port, timeout=30)
    encoded = (
        json.dumps(body, ensure_ascii=False).encode()
        if isinstance(body, dict)
        else body
    )
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    data = response.read()
    content_type = response.getheader("Content-Type", "")
    connection.close()
    return (
        response.status,
        json.loads(data) if content_type.startswith("application/json") else data,
    )


def _draft_envelope(review: Any, body: dict[str, object]) -> dict[str, object]:
    status, snapshot = _http(review, "GET", "/api/workflow/draft-editor")
    assert status == 200, snapshot
    workspace = snapshot["workspace"]
    generation = workspace["expected_checkpoint_ref"]["generation"]
    return {
        **body,
        "operation_id": f"dwop_{generation}_{secrets.token_hex(16)}",
        "expected_checkpoint_ref": workspace["expected_checkpoint_ref"],
        "expected_current_candidate_ref": workspace[
            "expected_current_candidate_ref"
        ],
    }


def _cli_review_check(project_path: Path) -> str:
    """Start the documented CLI review entry and require a served HTTP 200.

    Mirrors a browser: the CLI prints the tokenized URL, loading it stores the
    session cookie, and the same cookie authorizes ``/api/review``.
    """

    command = [
        str(REPO_ROOT / "core" / ".venv" / "bin" / "roughcut"),
        "review",
        str(project_path),
        "--run-id",
        RUN_ID,
        "--json",
    ]
    environment = dict(os.environ, PYTHONPATH=f"{REPO_ROOT / 'core' / 'src'}:{REPO_ROOT}")
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_line: list[str] = []
    reader = threading.Thread(
        target=lambda: first_line.append(process.stdout.readline()),  # type: ignore[union-attr]
        daemon=True,
    )
    reader.start()
    reader.join(timeout=30)
    if not first_line:
        process.terminate()
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"CLI review did not start: {stderr.strip()}")
    payload = json.loads(first_line[0])
    assert payload.get("ok") is True, payload
    url = str(payload["review"]["url"])
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        with opener.open(url, timeout=15) as index_response:
            assert index_response.status == 200
            index_body = index_response.read()
            assert b"roughcut" in index_body.lower()
        with opener.open(urllib.parse.urljoin(url, "api/review"), timeout=15) as review_response:
            assert review_response.status == 200
            review = json.loads(review_response.read())
        assert review["basis"]["type"] == "decision", review["basis"]
        assert any(
            source["display_name"] == "解说待配音" for source in review["sources"]
        ), review["sources"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    # Report only the origin: the tokenized URL is single-run and already gone.
    return urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()


def _fcpxml_seconds(value: str | None) -> int:
    assert value is not None and value.endswith("s"), value
    text = value[:-1]
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        return round(int(numerator) / int(denominator) * TICKS)
    return round(float(text) * TICKS)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_frames_and_duration(path: Path) -> tuple[int, int]:
    _ffmpeg, ffprobe = _tools()
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    data = json.loads(result.stdout)
    frames = int(data["streams"][0]["nb_read_frames"])
    duration_ticks = round(float(data["format"]["duration"]) * TICKS)
    return frames, duration_ticks


def _signal_stats(path: Path, at_seconds: float) -> dict[str, float]:
    ffmpeg, _ffprobe = _tools()
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-ss",
            f"{at_seconds:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "signalstats,metadata=print:file=-",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    values: dict[str, float] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"lavfi\.signalstats\.(YAVG|YMAX|YMIN)=([-0-9.]+)", line)
        if match:
            values[match.group(1)] = float(match.group(2))
    assert "YAVG" in values and "YMAX" in values, result.stdout
    return values


def _window_loudness(path: Path, start_seconds: float, end_seconds: float) -> float:
    ffmpeg, _ffprobe = _tools()
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-ss",
            f"{start_seconds:.3f}",
            "-t",
            f"{end_seconds - start_seconds:.3f}",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    match = re.search(r"mean_volume:\s*([-0-9.]+|-inf) dB", result.stderr)
    assert match is not None, result.stderr
    return float("-inf") if match.group(1) == "-inf" else float(match.group(1))


def _runtime(tools: tuple[str, str]) -> Any:
    from types import SimpleNamespace

    ffmpeg, ffprobe = tools
    ffmpeg_version = subprocess.run(
        [ffmpeg, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    ffprobe_version = subprocess.run(
        [ffprobe, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    return SimpleNamespace(
        runtime_binding_sha256="a" * 64,
        python_receipt_hash="b" * 64,
        ffmpeg_tool_selection_hash="c" * 64,
        ffprobe_tool_selection_hash="d" * 64,
        ffmpeg=ToolResolution(ffmpeg, ffmpeg, ffmpeg_version),
        ffprobe=ToolResolution(ffprobe, ffprobe, ffprobe_version),
    )


def _build_project(root: Path) -> dict[str, object]:
    """Create the deterministic two-camera project and reach draft_review."""
    media_dir = root.parent / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    main_path = media_dir / "main.mp4"
    aux_path = media_dir / "aux.mp4"
    _make_marked_source(main_path, MAIN_SEGMENT_COLORS)
    _make_marked_source(aux_path, AUX_SEGMENT_COLORS)

    project = create_project(root, "未配音解说占位验收样片")
    project = add_source(root, main_path, ImportMode.LINKED, expected_revision=project.revision)
    project = add_source(root, aux_path, ImportMode.LINKED, expected_revision=project.revision)
    main_source = next(source for source in project.sources if source.display_name == "main.mp4")
    aux_source = next(source for source in project.sources if source.display_name == "aux.mp4")

    segments_a = (
        _segment("seg_a0", 0, 2 * TICKS, "甲的开场内容。", "spk_0"),
        _segment("seg_a1", 2 * TICKS, 4 * TICKS, "乙的正文内容。", "spk_1"),
        _segment("seg_a2", 4 * TICKS, 6 * TICKS, "未映射人物的话。", "spk_2"),
    )
    segments_b = (_segment("seg_b0", 0, 2 * TICKS, "乙的正文内容。", "spk_0"),)
    write_new_json(
        root / "transcripts" / main_source.source_id / "tr_a.json",
        _transcript(main_source.source_id, "tr_a", segments_a).to_dict(),
    )
    write_new_json(
        root / "transcripts" / aux_source.source_id / "tr_b.json",
        _transcript(aux_source.source_id, "tr_b", segments_b).to_dict(),
    )
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={
                main_source.source_id: "tr_a",
                aux_source.source_id: "tr_b",
            },
            persons=(
                Person("person_a", "人物甲", "主持人", ""),
                Person("person_b", "人物乙", "老师", ""),
            ),
            speaker_maps=(
                SpeakerMap(main_source.source_id, "tr_a", "spk_0", "person_a", True),
                SpeakerMap(main_source.source_id, "tr_a", "spk_1", "person_b", True),
            ),
        ),
        expected_revision=project.revision,
    )

    started = workflow_start(
        root, RUN_ID, [main_source.source_id, aux_source.source_id]
    )
    scope_basis = started.status["confirmation_bases"]["scope"]["basis"]
    scoped = workflow_action(
        root,
        RUN_ID,
        "act_sample_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": scope_basis,
            "source_authorizations": [
                {
                    "source_id": source_id,
                    "transcribe": False,
                    "speaker_diarization": False,
                }
                for source_id in (main_source.source_id, aux_source.source_id)
            ],
            "multicam_setup": _multicam_setup(
                main_source.source_id, aux_source.source_id
            ),
        },
    )
    status = workflow_action(
        root,
        RUN_ID,
        "act_sample_brief",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": scoped.status["confirmation_bases"]["brief"][
                "basis"
            ],
            "theme": "占位验收",
            "target_duration_ticks": 1_680_000,
            "focus": ["未配音解说"],
            "allow_reorder": True,
            "speaker_resolution_waivers": [
                {
                    "source_id": main_source.source_id,
                    "transcript_version_id": "tr_a",
                    "local_speaker_id": "spk_2",
                },
                {
                    "source_id": aux_source.source_id,
                    "transcript_version_id": "tr_b",
                    "local_speaker_id": "spk_0",
                },
            ],
        },
    )
    status = workflow_action(
        root,
        RUN_ID,
        "act_sample_outline",
        "submit_outline",
        {
            "schema_version": 1,
            "title": "占位验收",
            "opening": "开场",
            "sections": [
                {
                    "section_id": "section_opening",
                    "title": "开场",
                    "summary": "开场",
                    "target_duration_ticks": 600_000,
                },
                {
                    "section_id": "section_body",
                    "title": "主体",
                    "summary": "主体",
                    "target_duration_ticks": 600_000,
                },
            ],
            "ending": "结尾",
            "required_content_coverage": [],
            "narration_status": "none",
        },
    )
    outline_ref = status.status["presented_subjects"]["outline_ref"]
    approved = workflow_action(
        root,
        RUN_ID,
        "act_sample_outline_approve",
        "approve_outline",
        {"schema_version": 1, "outline_ref": outline_ref},
    )
    assert approved.workflow_run.stage == "draft_review"
    return {
        "project_path": root,
        "main_source_id": main_source.source_id,
        "aux_source_id": aux_source.source_id,
        "main_source_path": main_path,
        "aux_source_path": aux_path,
    }


def _continuation_alignment(
    root: Path,
) -> tuple[MulticamAlignmentArtifact, dict[str, object]]:
    """Return the exact alignment the workflow continuation produced for the run."""
    status = workflow_status(root, RUN_ID)
    continuation = status["multicam_alignment"]
    alignment_ref = continuation.get("alignment_ref")
    assert continuation["status"] == "succeeded", continuation
    assert isinstance(alignment_ref, dict), continuation
    artifact = AlignmentStore(root).read(str(alignment_ref["alignment_id"]))
    assert artifact is not None
    assert artifact.content_hash == alignment_ref["content_hash"]
    return artifact, dict(alignment_ref)


def _checks() -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []

    def ok(name: str, detail: str) -> None:
        results.append((name, detail))

    output = Path(
        os.environ.get("ROUGHCUT_ACCEPTANCE_DIR", str(DEFAULT_OUTPUT))
    ).resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    root = output / "project"
    handoffs = output / "handoffs"
    handoffs.mkdir()

    sample = _build_project(root)
    main_source_id = cast(str, sample["main_source_id"])
    aux_source_id = cast(str, sample["aux_source_id"])
    ok("project", f"build {root}")

    review = start_review_server(
        root,
        source_bindings=[
            {"source_id": main_source_id, "transcript_version_id": "tr_a"},
            {"source_id": aux_source_id, "transcript_version_id": "tr_b"},
        ],
    )
    try:
        status, created = _http(
            review,
            "POST",
            "/api/workflow/content-drafts",
            body={
                "blocks": [
                    {"block_id": "sample_section_open", "kind": "section_title", "title": "开场"},
                    {
                        "block_id": "sample_a0",
                        "kind": "source_excerpt",
                        "refs": [
                            {
                                "source_id": main_source_id,
                                "transcript_version_id": "tr_a",
                                "segment_id": "seg_a0",
                                "start_ticks": 0,
                                "end_ticks": 2 * TICKS,
                            }
                        ],
                        "canonical_text": "甲的开场内容。",
                    },
                    {
                        "block_id": "sample_n1",
                        "kind": "narration",
                        "text": "这段解说还没有配音。",
                        "status": "draft",
                        "recorded_refs": [],
                    },
                    {"block_id": "sample_section_body", "kind": "section_title", "title": "主体"},
                    {
                        "block_id": "sample_b0",
                        "kind": "source_excerpt",
                        "refs": [
                            {
                                "source_id": main_source_id,
                                "transcript_version_id": "tr_a",
                                "segment_id": "seg_a1",
                                "start_ticks": 2 * TICKS,
                                "end_ticks": 4 * TICKS,
                            }
                        ],
                        "canonical_text": "乙的正文内容。",
                    },
                    {
                        "block_id": "sample_a1",
                        "kind": "source_excerpt",
                        "refs": [
                            {
                                "source_id": main_source_id,
                                "transcript_version_id": "tr_a",
                                "segment_id": "seg_a2",
                                "start_ticks": 4 * TICKS,
                                "end_ticks": 6 * TICKS,
                            }
                        ],
                        "canonical_text": "未映射人物的话。",
                    },
                    {
                        "block_id": "sample_n2",
                        "kind": "narration",
                        "text": "还没配音的人工时长。",
                        "status": "draft",
                        "recorded_refs": [],
                    },
                ]
            },
            write=True,
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][
            "content_draft_id"
        ]

        editor_status, editor = _http(review, "GET", "/api/workflow/draft-editor")
        assert editor_status == 200, editor
        bylines = [
            paragraph["bylines"]
            for paragraph in editor["paragraphs"]
            if paragraph["kind"] == "source_excerpt"
        ]
        assert bylines == [
            [{"start_offset": 0, "end_offset": 7, "text": "人物甲 · 主持人"}],
            [{"start_offset": 0, "end_offset": 7, "text": "人物乙 · 老师"}],
            [{"start_offset": 0, "end_offset": 8, "text": "未命名发言人 · main.mp4 · spk_2"}],
        ], bylines
        ok("byline", "A1/A2 展示身份：已映射人物 + 未映射临时编号，无第二套编辑状态")

        narration_paragraph = next(
            paragraph
            for paragraph in editor["paragraphs"]
            if paragraph.get("block_id") == "sample_n2"
        )
        status, saved = _http(
            review,
            "POST",
            "/api/workflow/draft-narration",
            body=_draft_envelope(
                review,
                {
                    "candidate_id": candidate_id,
                    "block_id": "sample_n2",
                    "text": "还没配音的人工时长。",
                    "estimated_duration_seconds": 5,
                },
            ),
            write=True,
        )
        assert status == 201, saved
        edited = next(
            paragraph
            for paragraph in saved["draft_editor"]["paragraphs"]
            if paragraph.get("block_id") == "sample_n2"
        )
        assert edited["narration_duration_seconds"] == 5
        assert edited["narration_estimated_seconds"] == 3
        assert narration_paragraph["narration_duration_seconds"] is None
        ok("narration_edit", "A3 解说编辑与人工时长保存回读（自动估算保持独立）")

        current_status, current_editor = _http(
            review, "GET", "/api/workflow/draft-editor"
        )
        assert current_status == 200, current_editor
        current_candidate_id = current_editor["candidate"]["candidate_id"]
        assert current_candidate_id != candidate_id
        confirm_body = _draft_envelope(
            review, {"content_draft_id": current_candidate_id}
        )
        status, confirmed = _http(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            body=confirm_body,
            write=True,
        )
        assert status == 200, confirmed
        confirmed_id = confirmed["content_draft_mutation"]["content_draft"][
            "content_draft_id"
        ]
        status, proposed = _http(
            review,
            "POST",
            "/api/workflow/content-draft-propose",
            body={"content_draft_id": confirmed_id},
            write=True,
        )
        assert status == 200, proposed
        proposal = proposed["content_draft_proposal"]["proposal"]
        review_payload = proposed["review"]
        placeholder_clips = [
            clip for clip in proposal["clips"] if clip.get("kind") == "narration_placeholder"
        ]
        assert len(placeholder_clips) == 2, proposal["clips"]
        media_ids = {clip["media_id"] for clip in placeholder_clips}
        assert len(media_ids) == 1
        media_id = media_ids.pop()
        assert {clip["source_out_ticks"] for clip in placeholder_clips} == {
            3 * TICKS,
            5 * TICKS,
        }
        assert all(clip["media_frames"] == 5 * 25 + 2 for clip in placeholder_clips)
        placeholder_sources = [
            source
            for source in review_payload["sources"]
            if source["source_id"] == media_id
        ]
        assert len(placeholder_sources) == 1
        assert placeholder_sources[0]["display_name"] == "解说待配音"
        placeholder_spans = [
            span
            for span in review_payload["timeline"]["spans"]
            if span["source_id"] == media_id
        ]
        assert len(placeholder_spans) == 2
        assert [
            span["output_out_ticks"] - span["output_in_ticks"]
            for span in placeholder_spans
        ] == [3 * TICKS, 5 * TICKS]
        media_path = placeholder_media_path(root, media_id)
        assert media_path.is_file()
        record = read_placeholder_media(root, media_id)
        assert record is not None
        ok(
            "proposal_placeholder",
            "A3/A4 唯一剪辑序列 + 共享占位媒体（3s/5s 同源裁切）",
        )

        status, media_bytes = _http(
            review,
            "GET",
            f"/media/{media_id}",
            extra_headers={"Range": "bytes=0-1023"},
        )
        assert status in {200, 206}, status
        assert isinstance(media_bytes, bytes) and len(media_bytes) > 0
        ok("media_endpoint", "A4 preview /media 授权读取占位媒体成功")

        status, adopted = _http(
            review,
            "POST",
            "/api/roughcut-adopt",
            body={
                "basis_id": proposal["proposal_id"],
                "expected_revision": review_payload["project"]["revision"],
            },
            write=True,
        )
        assert status == 200, adopted
        decision_id = adopted["review"]["basis"]["id"]
    finally:
        review.close()

    project = ProjectStore(root).load()
    decision = load_review_snapshot(root, edit_version_id=decision_id)
    placeholder_clip_ids = {clip["clip_id"] for clip in placeholder_clips}
    decision_placeholders = [
        clip
        for clip in decision.proposal.clips
        if clip.clip_id in placeholder_clip_ids
    ]
    assert len(decision_placeholders) == 2
    assert all(
        isinstance(clip, NarrationPlaceholderClip)
        for clip in decision_placeholders
    )
    assert {clip.media_id for clip in decision_placeholders} == {media_id}
    total_ticks = decision.timeline.total_duration_ticks
    assert total_ticks == 14 * TICKS
    assert [span.output_in_ticks for span in decision.timeline.spans] == [
        0,
        2 * TICKS,
        5 * TICKS,
        7 * TICKS,
        9 * TICKS,
    ]
    assert [span.output_out_ticks for span in decision.timeline.spans] == [
        2 * TICKS,
        5 * TICKS,
        7 * TICKS,
        9 * TICKS,
        14 * TICKS,
    ]
    ok("decision_preview", f"A3/A4 采用后预览总时长 {total_ticks} ticks，跨度连续")

    output_settings = OutputSettings.from_dict(project.settings)
    render_result = render_roughcut(
        root, edit_version_id=decision_id, expected_revision=project.revision
    )
    rendered = root / render_result.mp4_path
    frames, duration_ticks = _probe_frames_and_duration(rendered)
    assert frames == 14 * 25
    assert duration_ticks == 14 * TICKS
    placeholder_window = (2.1, 4.9)
    stats = _signal_stats(rendered, (placeholder_window[0] + placeholder_window[1]) / 2)
    assert stats["YAVG"] < 40, stats
    assert stats["YMAX"] > 200, stats
    narration_loudness = _window_loudness(rendered, *placeholder_window)
    assert narration_loudness <= -70, narration_loudness
    mapped_loudness = _window_loudness(rendered, 0.2, 1.8)
    assert mapped_loudness > -40, mapped_loudness
    ok(
        "main_render",
        "A5 主输出 350 帧/14s，占位黑底白字标签可见、解码静音，真实素材音轨正常",
    )

    _alignment, alignment_ref = _continuation_alignment(root)
    import roughcut.application.multicam_parallel as parallel_module

    runtime = _runtime(_tools())

    prepared = prepare_multicam_parallel_render(
        root,
        edit_version_id=decision_id,
        alignment_ref=alignment_ref,
        auxiliary_camera_ids=["aux_1"],
        expected_revision=project.revision,
    )
    camera_plan = prepared.summary["cameras"][0]
    assert camera_plan["placeholder_ticks"] == 8 * TICKS
    assert (
        camera_plan["mapped_ticks"]
        + camera_plan["missing_ticks"]
        + camera_plan["uncertain_ticks"]
        + camera_plan["conflict_ticks"]
        + camera_plan["placeholder_ticks"]
        == total_ticks
    )
    classifications = [slot["classification"] for slot in camera_plan["slots"]]
    assert classifications.count("narration_placeholder") == 2
    placeholder_slots = [
        slot
        for slot in camera_plan["slots"]
        if slot["classification"] == "narration_placeholder"
    ]
    assert [slot["placeholder_ref"]["media_id"] for slot in placeholder_slots] == [
        media_id,
        media_id,
    ]
    ok("multicam_projection", "A4/A5 多机位 slot 显式分类，覆盖统计诚实计入占位")

    parallel_module_copy = parallel_module
    original_loader = parallel_module_copy._load_persistent_runtime
    parallel_module_copy._load_persistent_runtime = lambda: runtime
    try:
        outcome = start_multicam_parallel_render(
            root,
            operation_id="op_00000000000040008000000000000092",
            prepare_ref=prepared.prepare_ref,
        )
    finally:
        parallel_module_copy._load_persistent_runtime = original_loader
    assert outcome.result is not None, outcome.record
    manifest = json.loads(
        (
            root
            / "renders"
            / "multicam"
            / outcome.result["parallel_render_id"]
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    aux_output = (
        root
        / "renders"
        / "multicam"
        / outcome.result["parallel_render_id"]
        / "camera_aux_1.mp4"
    )
    assert aux_output.is_file()
    aux_frames, aux_duration = _probe_frames_and_duration(aux_output)
    assert aux_frames == frames
    assert aux_duration == duration_ticks
    assert _signal_stats(aux_output, 3.5)["YMAX"] > 200
    mapped_slots = [
        slot for slot in camera_plan["slots"] if slot["classification"] == "mapped"
    ]
    assert mapped_slots, "the deterministic alignment must map at least one interval"
    mapped_midpoint = (
        mapped_slots[0]["output_start_ticks"] + mapped_slots[0]["output_end_ticks"]
    ) / 2 / TICKS
    assert _signal_stats(aux_output, mapped_midpoint)["YAVG"] > 20
    assert _window_loudness(aux_output, *placeholder_window) <= -70
    assert manifest["cameras"][0]["placeholder_ticks"] == 8 * TICKS
    ok("multicam_render", "A5 副机位与主输出帧数/时长/切点一致，占位区间静音")

    expected_revision = ProjectStore(root).load().revision
    alignment_id = str(alignment_ref["alignment_id"])
    destination_fcpxml = handoffs / "sample.fcpxml"
    destination_fcp7 = handoffs / "sample.xml"
    fcpxml_outcome = approve_nle_export(
        root,
        run_id=RUN_ID,
        action_id="sample_nle_fcpxml",
        edit_version_id=decision_id,
        expected_revision=expected_revision,
        route="fcpxml",
        destination=destination_fcpxml,
        alignment_artifact_id=alignment_id,
    )
    fcp7_outcome = approve_nle_export(
        root,
        run_id=RUN_ID,
        action_id="sample_nle_fcp7",
        edit_version_id=decision_id,
        expected_revision=expected_revision,
        route="fcp7_xml",
        destination=destination_fcp7,
        alignment_artifact_id=alignment_id,
    )
    companion = placeholder_companion_directory(destination_fcpxml)
    delivered = companion / f"{media_id}.mp4"
    assert delivered.is_file()
    assert _sha256(delivered) == record.content_sha256
    binding = fcpxml_outcome.receipt.placeholder_media[0]
    assert binding.content_sha256 == record.content_sha256
    assert binding.duration_ticks == record.probe.duration_ticks
    assert fcp7_outcome.receipt.placeholder_media == fcpxml_outcome.receipt.placeholder_media

    fcpxml_text = destination_fcpxml.read_text(encoding="utf-8")
    assert delivered.as_uri() in fcpxml_text
    assert fcpxml_text.count(delivered.as_uri()) == 1
    assert fcpxml_text.count(f"{media_id}.mp4") == 1
    tree = ET.parse(destination_fcpxml)
    root_node = tree.getroot()
    assets = [
        element
        for element in root_node.iter("asset")
        if element.get("uid") == media_id
    ]
    assert len(assets) == 1
    placeholder_clip_elements = [
        element
        for element in root_node.iter("asset-clip")
        if element.get("ref") == f"asset-{media_id}"
    ]
    # Two main-track placeholders and their two aligned auxiliary lane clips.
    assert len(placeholder_clip_elements) == 4
    spines = list(root_node.iter("spine"))
    assert len(spines) == 1
    main_clips = [
        element for element in spines[0] if element.tag == "asset-clip"
    ]
    # The two contiguous original-media excerpts share a continuous auxiliary
    # mapping and export as one clip; canonical Decision still has five clips.
    assert len(main_clips) == 4
    merged_excerpt = main_clips[2]
    assert _fcpxml_seconds(merged_excerpt.get("start")) == 2 * TICKS
    assert _fcpxml_seconds(merged_excerpt.get("duration")) == 4 * TICKS
    assert len(merged_excerpt.findtext("note", "").split(" | ")[0].split(",")) == 2
    merged_aux = merged_excerpt.find("asset-clip")
    assert merged_aux is not None
    assert _fcpxml_seconds(merged_aux.get("start")) == 2 * TICKS
    assert _fcpxml_seconds(merged_aux.get("duration")) == 4 * TICKS
    cursor = 0
    for clip in main_clips:
        offset = _fcpxml_seconds(clip.get("offset"))
        duration = _fcpxml_seconds(clip.get("duration"))
        assert offset == cursor
        cursor += duration
    assert cursor == 14 * TICKS
    for clip in main_clips:
        if clip.get("ref") != f"asset-{media_id}":
            continue
        nested = [element for element in clip if element.tag == "asset-clip"]
        assert len(nested) == 1
        # Auxiliary lane clips are nested inside the main clip, so their
        # offset is relative to the parent and the two spans stay identical.
        assert _fcpxml_seconds(nested[0].get("offset")) == 0
        assert nested[0].get("duration") == clip.get("duration")
    fcp7_text = destination_fcp7.read_text(encoding="utf-8")
    assert f"file-{media_id}" in fcp7_text
    assert fcp7_text.count(f'<file id="file-{media_id}">') == 1
    assert fcp7_text.count(delivered.as_uri()) == 1
    ok(
        "nle_exports",
        "A6 主轨无 gap、主副占位同区间；4 个占位 clip 共享 1 个媒体定义",
    )

    retry = approve_nle_export(
        root,
        run_id=RUN_ID,
        action_id="sample_nle_fcpxml",
        edit_version_id=decision_id,
        expected_revision=expected_revision,
        route="fcpxml",
        destination=destination_fcpxml,
        alignment_artifact_id=alignment_id,
    )
    assert retry.readback is True
    assert retry.receipt == fcpxml_outcome.receipt
    ok("nle_idempotent", "A7 同 action 重试回读成功且不重写交付")

    before_mtime = media_path.stat().st_mtime_ns
    ensured = ensure_narration_placeholder_media(
        root,
        output_settings=output_settings,
        video_frames=placeholder_clips[0]["media_frames"],
        required_duration_ticks=5 * TICKS,
    )
    assert ensured.media_id == media_id
    assert media_path.stat().st_mtime_ns == before_mtime
    ok("placeholder_cached", "A7 已存在媒体零 FFmpeg 复用")

    original_bytes = delivered.read_bytes()
    try:
        delivered.write_bytes(original_bytes + b"tamper")
        with pytest.raises(NleHandoffError, match="placeholder media content changed"):
            approve_nle_export(
                root,
                run_id=RUN_ID,
                action_id="sample_nle_fcpxml",
                edit_version_id=decision_id,
                expected_revision=expected_revision,
                route="fcpxml",
                destination=destination_fcpxml,
                alignment_artifact_id=alignment_id,
            )
        delivered.unlink()
        with pytest.raises(NleHandoffError, match="placeholder media is missing"):
            approve_nle_export(
                root,
                run_id=RUN_ID,
                action_id="sample_nle_fcpxml",
                edit_version_id=decision_id,
                expected_revision=expected_revision,
                route="fcpxml",
                destination=destination_fcpxml,
                alignment_artifact_id=alignment_id,
            )
    finally:
        delivered.write_bytes(original_bytes)
    ok("nle_tamper", "A7 缺失或篡改 companion 媒体 fail closed")

    generated_bytes = media_path.read_bytes()
    generated_stat = media_path.stat()
    try:
        media_path.write_bytes(generated_bytes + b"tamper")
        with pytest.raises(ProjectError, match="fingerprint has changed"):
            prepare_render_plan(
                root, edit_version_id=decision_id, expected_revision=expected_revision
            )
    finally:
        media_path.write_bytes(generated_bytes)
        # The frozen fingerprint also binds the recorded mtime, so restore it.
        os.utime(
            media_path,
            ns=(generated_stat.st_atime_ns, generated_stat.st_mtime_ns),
        )
    ok("placeholder_tamper", "A7 项目内生成媒体被篡改时渲染计划 fail closed")

    # Preview entry on the untouched main sample: the adopted Decision is still
    # active and the review snapshot resolves the managed placeholder media.
    sample_final_revision = ProjectStore(root).load().revision
    preview = load_review_snapshot(root, edit_version_id=decision_id)
    assert any(
        source["source_id"] == media_id
        for source in preview.sources
    )
    assert any(
        selection.source_id == media_id
        for selection in preview.playback_selections
    )
    ok("preview_entry", "A3 决策预览快照包含占位素材与播放授权")

    # Direct-edit lineage, trim bounds, history readback and Review restore
    # change the Project revision, so they run on an identical copy: the
    # delivered sample must keep the exact revision/refs its workflow run and
    # multicam continuation bind. Linked synthetic media locators stay valid
    # because they are absolute paths outside the project directory.
    lineage_root = output / "lineage-project"
    shutil.copytree(root, lineage_root)
    from roughcut.application.edits import change_edit, read_edit_history, redo_edit, undo_edit

    placeholder_clip_id = decision_placeholders[0].clip_id
    ordered_ids = [clip.clip_id for clip in decision.proposal.clips]
    expected_revision = ProjectStore(lineage_root).load().revision
    reordered = change_edit(
        lineage_root,
        operation={
            "type": "reorder",
            "ordered_clip_ids": [
                *[clip_id for clip_id in ordered_ids if clip_id != placeholder_clip_id],
                placeholder_clip_id,
            ],
        },
        expected_revision=expected_revision,
        base_edit_version_id=decision_id,
    )
    assert reordered.changed is True
    reordered_placeholder = next(
        clip
        for clip in reordered.decision.proposal_snapshot.clips
        if clip.clip_id == placeholder_clip_id
    )
    assert isinstance(reordered_placeholder, NarrationPlaceholderClip)
    assert reordered_placeholder.media_id == media_id

    expected_revision = ProjectStore(lineage_root).load().revision
    deleted = change_edit(
        lineage_root,
        operation={"type": "delete", "clip_id": placeholder_clip_id},
        expected_revision=expected_revision,
        base_edit_version_id=reordered.decision.edit_version_id,
    )
    assert all(
        clip.clip_id != placeholder_clip_id
        for clip in deleted.decision.proposal_snapshot.clips
    )
    expected_revision = ProjectStore(lineage_root).load().revision
    restored = change_edit(
        lineage_root,
        operation={
            "type": "restore",
            "clip_id": placeholder_clip_id,
            "insert_before_clip_id": None,
        },
        expected_revision=expected_revision,
        base_edit_version_id=deleted.decision.edit_version_id,
    )
    restored_placeholder = next(
        clip
        for clip in restored.decision.proposal_snapshot.clips
        if clip.clip_id == placeholder_clip_id
    )
    assert isinstance(restored_placeholder, NarrationPlaceholderClip)
    assert restored_placeholder.media_id == media_id
    assert restored_placeholder.media_frames == decision_placeholders[0].media_frames

    expected_revision = ProjectStore(lineage_root).load().revision
    undone = undo_edit(
        lineage_root,
        expected_revision=expected_revision,
        base_edit_version_id=restored.decision.edit_version_id,
    )
    undone_snapshot = load_review_snapshot(
        lineage_root, edit_version_id=undone.active_edit_version_id
    )
    assert (
        undone_snapshot.timeline.total_duration_ticks
        == total_ticks - decision_placeholders[0].duration_ticks
    )
    assert all(
        isinstance(clip, NarrationPlaceholderClip)
        and clip.media_id == media_id
        for clip in undone_snapshot.proposal.clips
        if clip.clip_id != placeholder_clip_id
        and clip.clip_id in placeholder_clip_ids
    )
    expected_revision = ProjectStore(lineage_root).load().revision
    redone = redo_edit(
        lineage_root,
        expected_revision=expected_revision,
        base_edit_version_id=undone.active_edit_version_id,
    )
    redone_snapshot = load_review_snapshot(
        lineage_root, edit_version_id=redone.active_edit_version_id
    )
    redone_placeholder = next(
        clip
        for clip in redone_snapshot.proposal.clips
        if clip.clip_id == placeholder_clip_id
    )
    assert isinstance(redone_placeholder, NarrationPlaceholderClip)
    assert redone_placeholder.media_id == media_id
    ok("placeholder_lineage", "A3/A7 副本中占位在重排/删除/恢复/撤销/重做中原身份保留")

    # The public history readback and the Review change endpoint must describe a
    # restorable placeholder explicitly, and trims must stay inside its media.
    coverage_ticks = decision_placeholders[0].media_frames * (TICKS // 25)
    current_revision = ProjectStore(lineage_root).load().revision
    with pytest.raises(ProjectError, match="exceeds its generated media range"):
        change_edit(
            lineage_root,
            operation={
                "type": "trim",
                "clip_id": placeholder_clip_id,
                "source_in_ticks": 0,
                "source_out_ticks": coverage_ticks + TICKS,
            },
            expected_revision=current_revision,
            base_edit_version_id=redone.active_edit_version_id,
        )
    trimmed = change_edit(
        lineage_root,
        operation={
            "type": "trim",
            "clip_id": placeholder_clip_id,
            "source_in_ticks": 0,
            "source_out_ticks": coverage_ticks,
        },
        expected_revision=current_revision,
        base_edit_version_id=redone.active_edit_version_id,
    )
    assert trimmed.changed is True

    deleted_for_history = change_edit(
        lineage_root,
        operation={"type": "delete", "clip_id": placeholder_clip_id},
        expected_revision=ProjectStore(lineage_root).load().revision,
        base_edit_version_id=trimmed.decision.edit_version_id,
    )
    history = read_edit_history(lineage_root)
    assert [clip["clip_id"] for clip in history.restorable_clips] == [
        placeholder_clip_id
    ]
    summary = history.restorable_clips[0]
    assert summary["kind"] == "narration_placeholder"
    assert summary["narration_block_id"] == "sample_n1"
    assert summary["media_id"] == media_id
    assert "source_id" not in summary
    assert "segment_id" not in summary

    lineage_review = start_review_server(
        lineage_root, edit_version_id=deleted_for_history.decision.edit_version_id
    )
    try:
        status, state = _http(lineage_review, "GET", "/api/roughcut-state")
        assert status == 200, state
        status, restored_review = _http(
            lineage_review,
            "POST",
            "/api/roughcut-change",
            body={
                "basis_id": deleted_for_history.decision.edit_version_id,
                "expected_revision": state["review"]["project"]["revision"],
                "operation": {
                    "type": "restore",
                    "clip_id": placeholder_clip_id,
                    "insert_before_clip_id": None,
                },
            },
            write=True,
        )
        assert status == 201, restored_review
        assert placeholder_clip_id in [
            clip["clip_id"]
            for clip in restored_review["review"]["proposal"]["clips"]
        ]
        status, changed_again = _http(
            lineage_review,
            "POST",
            "/api/roughcut-change",
            body={
                "basis_id": restored_review["review"]["basis"]["id"],
                "expected_revision": restored_review["review"]["project"]["revision"],
                "operation": {"type": "delete", "clip_id": placeholder_clip_id},
            },
            write=True,
        )
        assert status == 201, changed_again
        restorable = [
            clip
            for clip in changed_again["candidate_history"]["restorable_clips"]
            if clip["clip_id"] == placeholder_clip_id
        ]
        assert len(restorable) == 1
        assert restorable[0]["kind"] == "narration_placeholder"
        assert restorable[0]["media_id"] == media_id
        assert "source_id" not in restorable[0]
    finally:
        lineage_review.close()
    ok(
        "placeholder_history",
        "R3 副本中删除后历史/Review 恢复摘要显式带 kind/media_id，trim 不越生成媒体范围",
    )
    ok(
        "lineage_isolation",
        "R6 编辑历史/trim/恢复检查在 output/lineage-project 副本执行，主样品绑定不变",
    )

    # The delivered sample must keep the exact state its run and multicam
    # continuation bind, and the documented review entry must really open.
    delivered = ProjectStore(root).load()
    assert delivered.revision == sample_final_revision
    assert delivered.active_edit_version_id == decision_id
    cli_url = _cli_review_check(root)
    ok("final_cli_review", f"R6 真实 CLI review --run-id 启动并 HTTP 200（{cli_url}）")

    report = {
        "output": str(output),
        "project": str(root),
        "lineage_project": str(lineage_root),
        "review_entry": f"roughcut review {root} --run-id {RUN_ID} --json",
        "media_id": media_id,
        "total_ticks": total_ticks,
        "checks": [{"name": name, "detail": detail} for name, detail in results],
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return results


def test_narration_placeholder_acceptance_sample(
    synthetic_media_runtime: tuple[str, str],
) -> None:
    results = _checks()
    names = {name for name, _detail in results}
    assert {
        "project",
        "byline",
        "narration_edit",
        "proposal_placeholder",
        "media_endpoint",
        "decision_preview",
        "main_render",
        "multicam_projection",
        "multicam_render",
        "nle_exports",
        "nle_idempotent",
        "placeholder_cached",
        "nle_tamper",
        "placeholder_tamper",
        "placeholder_lineage",
        "placeholder_history",
        "lineage_isolation",
        "preview_entry",
        "final_cli_review",
    } <= names
