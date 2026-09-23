from __future__ import annotations

import http.client
import json
import secrets
import socket
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.application.content_drafts as content_drafts_module
import roughcut.application.draft_editor as draft_editor_module
import roughcut.review.server as review_server_module
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.agent_context import (
    read_agent_context,
    read_multi_source_agent_context,
)
from roughcut.application.content_drafts import (
    create_content_draft,
    revise_content_draft_scoped,
)
from roughcut.application.projects import create_project
from roughcut.application.proposals import (
    create_multi_source_edit_proposal,
)
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflows import (
    workflow_action,
    workflow_start,
    workflow_status,
)
from roughcut.domain.edit import MultiSourceEditProposal
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import ImportMode, MediaProbe, ProjectError, SourceAsset
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.review.server import start_review_server


def _source(
    source_id: str,
    media: Path,
    *,
    duration_ticks: int = 600_000,
) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="video",
        display_name=f"素材 {source_id}",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(media.resolve())},
        fingerprint=fingerprint_file(media),
        probe=MediaProbe(
            duration_ticks=duration_ticks,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264",
            width=320,
            height=180,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
        tags=(f"tag:{source_id}",),
        note=f"备注 {source_id}",
    )


def _segment(
    segment_id: str,
    start: int,
    end: int,
    text: str,
    speaker: str,
    *,
    fine_units: tuple[FineUnit, ...] = (),
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
        fine_units=fine_units,
        editorial_mark="unmarked",
    )


def _transcript(
    source_id: str,
    transcript_id: str,
    *,
    compound: bool = False,
    extra_segments: int = 0,
    ownership_case: str | None = None,
) -> TimedTranscript:
    if ownership_case is not None and source_id == "src_a":
        specs = (
            [
                ("a01", 0, 300_000, "前段区。", (
                    FineUnit("word", "前段", 0, 180_000, None),
                    FineUnit("character", "区", 270_000, 285_000, None),
                    FineUnit("character", "。", 285_000, 300_000, None),
                )),
                ("a02", 300_000, 422_727, "主持人说：“请大家", (
                    FineUnit("word", "主持人说：“请大家", 300_000, 422_727, None),
                )),
                ("a03", 422_727, 500_000, "落点", (
                    FineUnit("word", "落点", 422_727, 500_000, None),
                )),
            ]
            if ownership_case == "a"
            else [
                ("b01", 0, 280_000, "前文", (FineUnit("word", "前文", 0, 280_000, None),)),
                ("b02", 300_000, 600_000, "前情时间学习。", (
                    FineUnit("word", "前情", 300_000, 500_000, None),
                    FineUnit("character", "时", 528_571, 542_857, None),
                    FineUnit("character", "间", 542_857, 557_143, None),
                    FineUnit("character", "学", 557_143, 571_429, None),
                    FineUnit("character", "习", 571_429, 585_714, None),
                    FineUnit("character", "。", 585_714, 600_000, None),
                )),
            ]
        )
        segments = tuple(
            _segment(item, start, end, text, "spk_0", fine_units=units)
            for item, start, end, text, units in specs
        )
    elif compound and source_id in {"src_a", "src_b"}:
        segments = tuple(
            _segment(
                f"seg_{source_id[-1]}_{index}",
                index * 60_000,
                index * 60_000 + 40_000,
                f"{source_id[-1].upper()}{index}",
                "spk_0",
                fine_units=(
                    FineUnit(
                        "character",
                        source_id[-1].upper(),
                        index * 60_000,
                        index * 60_000 + 20_000,
                        None,
                    ),
                    FineUnit(
                        "character",
                        str(index),
                        index * 60_000 + 20_000,
                        index * 60_000 + 40_000,
                        None,
                    ),
                ),
            )
            for index in range(6)
        )
        if source_id == "src_a":
            segments = (
                *segments,
                _segment(
                    "seg_a_tail",
                    480_000,
                    540_000,
                    "收尾段",
                    "spk_1",
                    fine_units=tuple(
                        FineUnit(
                            "character",
                            character,
                            480_000 + index * 20_000,
                            480_000 + (index + 1) * 20_000,
                            None,
                        )
                        for index, character in enumerate("收尾段")
                    ),
                ),
            )
    elif source_id == "src_a":
        text = "你好😀世界。"
        fine = tuple(
            FineUnit("character", character, index * 20_000, (index + 1) * 20_000, None)
            for index, character in enumerate(text)
        )
        base_segments = (
            _segment("seg_a_emoji", 0, 120_000, text, "spk_0", fine_units=fine),
            _segment("seg_a_repeat_1", 130_000, 220_000, "重复。", "spk_0"),
            _segment("seg_a_repeat_2", 230_000, 320_000, "重复。", "spk_0"),
        )
        generated = tuple(
            _segment(
                f"seg_a_long_{index:03d}",
                1_200_000 + index * 480_000,
                1_260_000 + index * 480_000,
                f"长稿段落 {index:03d}。",
                "spk_0",
            )
            for index in range(extra_segments)
        )
        segments = (*base_segments, *generated)
    else:
        segments = (
            _segment(f"seg_{source_id}", 0, 120_000, f"{source_id} 内容。", "spk_0"),
        )
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture",
            "1",
            {"private": "/private/model"},
            {},
            f"raw-asr/{source_id}/private.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=segments,
    )


def _gap_boundary_transcript() -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_gap",
        source_id="src_a",
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture",
            "1",
            {},
            {},
            "raw-asr/src_a/gap.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=(
            _segment(
                "seg_gap",
                100,
                500,
                "甲，乙 丙。",
                "spk_0",
                fine_units=(
                    FineUnit("character", "甲", 100, 200, None),
                    FineUnit("character", " ", 200, 250, None),
                    FineUnit("character", "乙", 250, 350, None),
                    FineUnit("character", " ", 350, 380, None),
                    FineUnit("character", "丙", 380, 480, None),
                ),
            ),
        ),
    )


def _workflow_project(
    tmp_path: Path,
    *,
    with_brief: bool = True,
    compound: bool = False,
    extra_segments: int = 0,
    gap_boundary: bool = False,
    ownership_case: str | None = None,
    workflow_source_ids: tuple[str, ...] = ("src_b", "src_a"),
) -> dict[str, object]:
    root = tmp_path / "workflow review project"
    project = create_project(root, "Workflow Review")
    sources: list[SourceAsset] = []
    transcripts: dict[str, str] = {}
    media: dict[str, Path] = {}
    for source_id in ("src_a", "src_b", "src_c"):
        media_path = tmp_path / f"{source_id}.mp4"
        media_path.write_bytes(source_id.encode() * 128)
        transcript_id = "tr_gap" if gap_boundary and source_id == "src_a" else f"tr_{source_id[-1]}"
        transcript = (
            _gap_boundary_transcript()
            if gap_boundary and source_id == "src_a"
            else _transcript(
                source_id,
                transcript_id,
                compound=compound,
                extra_segments=extra_segments if source_id == "src_a" else 0,
                ownership_case=ownership_case,
            )
        )
        write_new_json(
            root / "transcripts" / source_id / f"{transcript_id}.json",
            transcript.to_dict(),
        )
        sources.append(
            _source(
                source_id,
                media_path,
                duration_ticks=(
                    1_200_000 + max(extra_segments, 1) * 480_000
                    if source_id == "src_a"
                    else 600_000
                ),
            )
        )
        transcripts[source_id] = transcript_id
        media[source_id] = media_path
    person_a = Person("person_a", "人物 A", "主持人", "")
    person_b = Person("person_b", "人物 B", "老师", "")
    person_tail = Person("person_tail", "收尾人物", "嘉宾", "")
    prepared = replace(
        project,
        revision=1,
        sources=tuple(sources),
        active_transcript_versions=transcripts,
        persons=(person_a, person_b, person_tail),
        speaker_maps=(
            SpeakerMap("src_a", transcripts["src_a"], "spk_0", "person_a", True),
            SpeakerMap(
                "src_b",
                transcripts["src_b"],
                "spk_0",
                "person_a" if compound else "person_b",
                True,
            ),
            *(
                (SpeakerMap("src_a", transcripts["src_a"], "spk_1", "person_tail", True),)
                if compound
                else ()
            ),
        ),
    )
    ProjectStore(root).save(prepared, expected_revision=0)
    project_ordered_ids = [
        source_id
        for source_id in ("src_a", "src_b", "src_c")
        if source_id in workflow_source_ids
    ]
    started = workflow_start(root, "wfr_review", project_ordered_ids)
    scope_basis = started.status["confirmation_bases"]["scope"]["basis"]
    scoped = workflow_action(
        root,
        "wfr_review",
        "act_review_scope",
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
                for source_id in workflow_source_ids
            ],
        },
    )
    if with_brief:
        brief_basis = scoped.status["confirmation_bases"]["brief"]["basis"]
        workflow_action(
            root,
            "wfr_review",
            "act_review_brief",
            "confirm_brief",
            {
                "schema_version": 1,
                "confirmation_basis": brief_basis,
                "theme": "Workflow 主题",
                "target_duration_ticks": 360_000,
                "focus": ["保留重点"],
                "allow_reorder": True,
                "speaker_resolution_waivers": [],
            },
        )
        outline = workflow_action(
            root,
            "wfr_review",
            "act_review_outline",
            "submit_outline",
            {
                "schema_version": 1,
                "title": "Workflow 主题",
                "opening": "开场",
                "sections": [
                    {
                        "section_id": f"section_{index}",
                        "title": title,
                        "summary": title,
                        "target_duration_ticks": 90_000,
                    }
                    for index, title in enumerate(
                        ("开场", "主体一", "主体二", "结尾"), start=1
                    )
                ],
                "ending": "结尾",
                "required_content_coverage": [],
                "narration_status": "none",
            },
        )
        outline_ref = outline.status["presented_subjects"]["outline_ref"]
        workflow_action(
            root,
            "wfr_review",
            "act_review_outline_approve",
            "approve_outline",
            {
                "schema_version": 1,
                "outline_ref": outline_ref,
            },
        )
    return {
        "root": root,
        "bindings": [
            {
                "source_id": source_id,
                "transcript_version_id": transcripts[source_id],
            }
            for source_id in workflow_source_ids
        ],
        "media": media,
    }


def _static(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("workflow", encoding="utf-8")
    return static


def _headers(review, *, write: bool = False) -> dict[str, str]:  # type: ignore[no-untyped-def]
    result = {"X-Roughcut-Token": review.token}
    if write:
        result.update(
            {
                "Origin": f"http://127.0.0.1:{review.port}",
                "Content-Type": "application/json",
            }
        )
    return result


def _request(
    review,  # type: ignore[no-untyped-def]
    method: str,
    path: str,
    *,
    body: dict[str, object] | bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object] | bytes]:
    workspace_paths = {
        "/api/workflow/content-draft-confirm",
        "/api/workflow/draft-edit",
        "/api/workflow/draft-narration",
        "/api/workflow/draft-candidate-select",
        "/api/workflow/draft-undo",
        "/api/workflow/draft-redo",
    }
    if (
        method == "POST"
        and path in workspace_paths
        and isinstance(body, dict)
        and "operation_id" not in body
    ):
        key = path + ":" + json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        remembered = getattr(review, "_test_workspace_envelopes", {})
        envelope = (
            remembered.get(key)
            if path == "/api/workflow/content-draft-confirm"
            else None
        )
        if envelope is None:
            snapshot_status, snapshot = _request(
                review,
                "GET",
                "/api/workflow/draft-editor",
            )
            assert snapshot_status == 200, snapshot
            assert isinstance(snapshot, dict)
            workspace = snapshot["workspace"]
            assert isinstance(workspace, dict)
            checkpoint_ref = workspace["expected_checkpoint_ref"]
            assert isinstance(checkpoint_ref, dict)
            generation = checkpoint_ref["generation"]
            assert isinstance(generation, int)
            envelope = {
                "operation_id": f"dwop_{generation}_{secrets.token_hex(16)}",
                "expected_checkpoint_ref": checkpoint_ref,
                "expected_current_candidate_ref": workspace[
                    "expected_current_candidate_ref"
                ],
            }
            if path == "/api/workflow/content-draft-confirm":
                remembered[key] = envelope
                review._test_workspace_envelopes = remembered
        body = {**body, **envelope}
    connection = http.client.HTTPConnection("127.0.0.1", review.port, timeout=2)
    encoded = (
        json.dumps(body, ensure_ascii=False).encode()
        if isinstance(body, dict)
        else body
    )
    connection.request(method, path, body=encoded, headers=headers or _headers(review))
    response = connection.getresponse()
    data = response.read()
    content_type = response.getheader("Content-Type", "")
    connection.close()
    return (
        response.status,
        json.loads(data) if content_type.startswith("application/json") else data,
    )


def _workspace_body(
    review,  # type: ignore[no-untyped-def]
    body: dict[str, object],
) -> dict[str, object]:
    status, snapshot = _request(review, "GET", "/api/workflow/draft-editor")
    assert status == 200, snapshot
    assert isinstance(snapshot, dict)
    workspace = snapshot["workspace"]
    assert isinstance(workspace, dict)
    checkpoint_ref = workspace["expected_checkpoint_ref"]
    assert isinstance(checkpoint_ref, dict)
    generation = checkpoint_ref["generation"]
    assert isinstance(generation, int)
    return {
        **body,
        "operation_id": f"dwop_{generation}_{secrets.token_hex(16)}",
        "expected_checkpoint_ref": checkpoint_ref,
        "expected_current_candidate_ref": workspace[
            "expected_current_candidate_ref"
        ],
    }


def _assert_edit_timing(
    payload: dict[str, object],
    operation: str,
) -> None:
    assert payload["operation"] == operation
    timing = payload["timing"]
    assert isinstance(timing, dict)
    assert set(timing) == {
        "selection_caret_revalidation_ms",
        "immutable_child_write_fsync_ms",
        "project_brief_transcript_context_validation_ms",
        "workflow_snapshot_refresh_ms",
        "draft_snapshot_rebuild_ms",
        "server_before_response_ms",
    }
    assert all(
        isinstance(value, (int, float)) and value >= 0
        for value in timing.values()
    )
    assert timing["server_before_response_ms"] <= 2_000


def test_workflow_startup_is_explicit_scoped_and_mutually_exclusive(tmp_path: Path) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    bindings = fixture["bindings"]
    with start_review_server(
        root,
        source_bindings=bindings,
        static_root=_static(tmp_path),
    ) as review:
        status, payload = _request(review, "GET", "/api/workflow")
        assert status == 200
        assert isinstance(payload, dict)
        assert payload["review_mode"] == "workflow"
        assert payload["source_bindings"] == bindings
        assert [source["source_id"] for source in payload["sources"]] == ["src_b", "src_a"]
        assert payload["session"]["status"] == "current"
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "src_c" not in serialized
        assert str(root) not in serialized
        assert str(tmp_path) not in serialized
        assert "locator" not in serialized
        assert "raw-asr" not in serialized
        assert review.token not in serialized
        assert _request(review, "GET", "/media/src_c")[0] == 404
        assert _request(review, "GET", "/media/src_b")[0] == 200

    with pytest.raises(ProjectError, match="mutually exclusive"):
        start_review_server(
            root,
            source_bindings=bindings,
            proposal_id="proposal_x",
            static_root=_static(tmp_path),
        )
    with pytest.raises(ProjectError, match="content_draft_id is invalid"):
        start_review_server(
            root,
            source_bindings=bindings,
            content_draft_id="../escape",
            static_root=_static(tmp_path),
        )
    with pytest.raises(ProjectError, match="binding fields"):
        start_review_server(
            root,
            source_bindings=[
                {**bindings[0], "locator": str(tmp_path)},
                bindings[1],
            ],
            static_root=_static(tmp_path),
        )


def test_workflow_brief_create_refreshes_revision_and_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path, with_brief=False)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        brief_request = {
            "theme": "新主题",
            "target_duration_ticks": 240_000,
            "focus": ["重点"],
            "allow_reorder": False,
        }
        status, payload = _request(review, "GET", "/api/workflow")
        assert status == 200
        assert payload["brief"] is None  # type: ignore[index]
        status, brief_state = _request(review, "GET", "/api/workflow/brief")
        assert status == 200
        assert brief_state["brief"] is None  # type: ignore[index]
        status, payload = _request(
            review,
            "POST",
            "/api/workflow/brief",
            headers=_headers(review, write=True),
            body=brief_request,
        )
        assert status == 201
        assert payload["brief_mutation"]["project_revision"] == 2  # type: ignore[index]
        assert payload["workflow"]["project"]["revision"] == 2  # type: ignore[index]
        first_receipt = payload["workflow_receipt"]  # type: ignore[index]
        status, repeated = _request(
            review,
            "POST",
            "/api/workflow/brief",
            headers=_headers(review, write=True),
            body=brief_request,
        )
        assert status == 201
        assert repeated["workflow_receipt"] == first_receipt  # type: ignore[index]
        assert repeated["brief_mutation"]["project_revision"] == 2  # type: ignore[index]
        assert not (root / "content-drafts").exists()
        assert not (root / "proposals").exists()
        assert not (root / "edits").exists()
        assert not (root / "renders").exists()
        status, payload = _request(
            review,
            "POST",
            "/api/workflow/brief",
            headers=_headers(review, write=True),
            body={
                "theme": "错误",
                "target_duration_ticks": 240_000,
                "focus": ["重点"],
                "allow_reorder": False,
                "source_bindings": [],
            },
        )
        assert status == 400
        assert payload["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]


def test_workflow_brief_save_failure_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _workflow_project(tmp_path, with_brief=False)
    root = Path(fixture["root"])
    initial = ProjectStore(root).load()
    original_save = ProjectStore.save

    def fail_save(self: ProjectStore, project, *, expected_revision: int) -> None:  # type: ignore[no-untyped-def]
        if project.active_brief_id is not None:
            raise OSError("injected project save failure")
        original_save(self, project, expected_revision=expected_revision)

    monkeypatch.setattr(ProjectStore, "save", fail_save)
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, payload = _request(
            review,
            "POST",
            "/api/workflow/brief",
            headers=_headers(review, write=True),
            body={
                "theme": "失败不应落盘",
                "target_duration_ticks": 25_200_000,
                "focus": ["解说方式：待定", "内容要求：保留真实互动"],
                "allow_reorder": True,
            },
        )

    assert status == 500
    assert payload["error"]["code"] == "review_service_failed"  # type: ignore[index]
    assert ProjectStore(root).load().revision == initial.revision
    assert ProjectStore(root).load().active_brief_id is None
    assert list((root / "briefs").glob("*.json")) == []


def test_workflow_readable_and_selection_reuse_frozen_bindings_and_code_points(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    with start_review_server(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, page = _request(
            review,
            "POST",
            "/api/workflow/readable-transcript",
            headers=_headers(review, write=True),
            body={"offset": 0, "limit": 20, "filters": {"source_ids": ["src_a"]}},
        )
        assert status == 200
        assert page["source_bindings"] == fixture["bindings"]  # type: ignore[index]
        assert page["total"] == 1  # type: ignore[index]
        paragraph = page["paragraphs"][0]  # type: ignore[index]
        assert "😀" in paragraph["text"]
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/selection-resolve",
            headers=_headers(review, write=True),
            body={
                "view_hash": page["view_hash"],  # type: ignore[index]
                "selections": [
                    {
                        "paragraph_id": paragraph["paragraph_id"],
                        "start_offset": 2,
                        "end_offset": 3,
                        "quote": "😀",
                        "occurrence": 0,
                    }
                ],
            },
        )
        assert status == 200, selection
        assert selection["mode"] == "exact"  # type: ignore[index]
        assert selection["canonical_text"] == "😀"  # type: ignore[index]
        assert selection["refs"][0]["start_ticks"] == 40_000  # type: ignore[index]
        assert selection["refs"][0]["end_ticks"] == 60_000  # type: ignore[index]

        text = paragraph["text"]
        second_repeat = text.rfind("重复。")
        status, expanded = _request(
            review,
            "POST",
            "/api/workflow/selection-resolve",
            headers=_headers(review, write=True),
            body={
                "view_hash": page["view_hash"],  # type: ignore[index]
                "selections": [
                    {
                        "paragraph_id": paragraph["paragraph_id"],
                        "start_offset": second_repeat + 1,
                        "end_offset": second_repeat + 2,
                        "quote": "复",
                        "occurrence": 1,
                    }
                ],
            },
        )
        assert status == 200
        assert expanded["mode"] == "expanded_to_segments"  # type: ignore[index]
        assert expanded["refs"][0]["segment_id"] == "seg_a_repeat_2"  # type: ignore[index]

        status, stale = _request(
            review,
            "POST",
            "/api/workflow/selection-resolve",
            headers=_headers(review, write=True),
            body={
                "view_hash": "0" * 64,
                "selections": [{"paragraph_id": paragraph["paragraph_id"]}],
            },
        )
        assert status == 400
        assert stale["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]

        status, error = _request(
            review,
            "POST",
            "/api/workflow/readable-transcript",
            headers=_headers(review, write=True),
            body={
                "offset": 0,
                "limit": 20,
                "source_bindings": [
                    {"source_id": "src_c", "transcript_version_id": "tr_c"}
                ],
            },
        )
        assert status == 400
        assert error["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]


def test_workflow_readable_supports_pagination_filters_and_existing_overlay(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    proposal = create_multi_source_edit_proposal(
        root,
        source_bindings=fixture["bindings"],
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        clips=[
            {
                "clip_id": "clip_overlay",
                "source_id": "src_a",
                "transcript_version_id": "tr_a",
                "segment_id": "seg_a_emoji",
                "source_in_ticks": 0,
                "source_out_ticks": 120_000,
                "reason": "overlay",
                "display_text": "你好😀世界。",
            }
        ],
        total_duration_ticks=120_000,
        expected_revision=project.revision,
    )
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, first = _request(
            review,
            "POST",
            "/api/workflow/readable-transcript",
            headers=_headers(review, write=True),
            body={"offset": 0, "limit": 1},
        )
        assert status == 200
        assert first["next_cursor"] == 1  # type: ignore[index]
        status, second = _request(
            review,
            "POST",
            "/api/workflow/readable-transcript",
            headers=_headers(review, write=True),
            body={"offset": 1, "limit": 1},
        )
        assert status == 200
        assert second["view_hash"] == first["view_hash"]  # type: ignore[index]
        status, overlaid = _request(
            review,
            "POST",
            "/api/workflow/readable-transcript",
            headers=_headers(review, write=True),
            body={
                "offset": 0,
                "limit": 20,
                "filters": {"source_ids": ["src_a"], "adoption_statuses": ["partial"]},
                "overlay": {
                    "basis": "proposal",
                    "artifact_id": proposal.proposal.proposal_id,
                },
            },
        )
        assert status == 200, overlaid
        assert overlaid["total"] == 1  # type: ignore[index]
        assert overlaid["paragraphs"][0]["adoption_status"] == "partial"  # type: ignore[index]


def _source_blocks() -> list[dict[str, object]]:
    return [
        {
            "block_id": "block_b",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_b",
                    "transcript_version_id": "tr_b",
                    "segment_id": "seg_src_b",
                    "start_ticks": 0,
                    "end_ticks": 120_000,
                }
            ],
            "canonical_text": "src_b 内容。",
        },
        {
            "block_id": "block_a",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_emoji",
                    "start_ticks": 0,
                    "end_ticks": 120_000,
                }
            ],
            "canonical_text": "你好😀世界。",
        },
    ]


def _schema2_section_blocks() -> list[dict[str, object]]:
    return [
        {
            "block_id": "heading_one",
            "kind": "section_title",
            "title": "第一章",
        },
        _source_blocks()[0],
        {
            "block_id": "heading_two",
            "kind": "section_title",
            "title": "第二章",
        },
        _source_blocks()[1],
    ]


def _gap_schema2_blocks() -> list[dict[str, object]]:
    return [
        {
            "block_id": "gap_heading",
            "kind": "section_title",
            "title": "原章节",
        },
        {
            "block_id": "gap_block",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_gap",
                    "segment_id": "seg_gap",
                    "start_ticks": 100,
                    "end_ticks": 500,
                }
            ],
            "canonical_text": "甲，乙 丙。",
        },
    ]


def _without_canonical(ref: dict[str, object]) -> dict[str, object]:
    return {
        key: ref[key]
        for key in (
            "source_id",
            "transcript_version_id",
            "segment_id",
            "start_ticks",
            "end_ticks",
        )
    }


def _schema2_display_point(
    editor: dict[str, object],
    display: dict[str, object],
) -> dict[str, object]:
    paragraph_id = display["paragraph_id"]
    character_offset = display["character_offset"]
    paragraphs = editor["paragraphs"]
    assert isinstance(paragraphs, list)
    paragraph = next(
        item for item in paragraphs
        if isinstance(item, dict) and item["paragraph_id"] == paragraph_id
    )
    assert isinstance(paragraph, dict)
    block_id = paragraph.get("block_id")
    runs = paragraph.get("source_runs", [])
    if isinstance(runs, list):
        for run in runs:
            if (
                isinstance(run, dict)
                and isinstance(run.get("block_id"), str)
                and isinstance(run.get("start_offset"), int)
                and isinstance(run.get("end_offset"), int)
                and run["start_offset"] <= character_offset <= run["end_offset"]
            ):
                block_id = run["block_id"]
                break
    assert isinstance(block_id, str)
    return {
        "paragraph_id": paragraph_id,
        "block_id": block_id,
        "utf16_offset": display["utf16_offset"],
    }


def _paragraph_block_id(paragraph: dict[str, object]) -> str:
    block_id = paragraph.get("block_id")
    if isinstance(block_id, str):
        return block_id
    for run in paragraph.get("source_runs", []):
        if isinstance(run, dict) and isinstance(run.get("block_id"), str):
            return run["block_id"]
    raise AssertionError("paragraph has no block identity")


def _schema2_drop_source(
    editor: dict[str, object],
    selection: dict[str, object],
) -> dict[str, object]:
    resolution = selection["resolution"]
    assert isinstance(resolution, dict)
    display = selection["display_range"]
    assert isinstance(display, dict)
    anchor = display["anchor"]
    focus = display["focus"]
    assert isinstance(anchor, dict) and isinstance(focus, dict)
    resolution_hash = selection.get("resolution_hash")
    assert isinstance(resolution_hash, str)
    narration_block_id = selection.get("narration_block_id")
    if isinstance(narration_block_id, str):
        refs = resolution["refs"]
        assert isinstance(refs, list)
        return {
            "kind": "narration_block",
            "surface": "draft",
            "block_id": narration_block_id,
            "text": selection["narration_text"],
            "status": selection["narration_status"],
            "recorded_refs": [
                _without_canonical(ref) for ref in refs if isinstance(ref, dict)
            ],
        }
    refs = resolution["refs"]
    assert isinstance(refs, list) and refs
    ref_segments = {
        (ref["source_id"], ref["transcript_version_id"], ref["segment_id"])
        for ref in refs
        if isinstance(ref, dict)
    }
    block_ids: list[str] = []
    for item in editor["paragraphs"]:
        if not isinstance(item, dict):
            continue
        for run in item.get("source_runs", []):
            if not isinstance(run, dict) or not isinstance(run.get("block_id"), str):
                continue
            run_refs = run.get("refs", [])
            if not isinstance(run_refs, list):
                continue
            matching = [
                r
                for r in run_refs
                if isinstance(r, dict)
                and (r.get("source_id"), r.get("transcript_version_id"), r.get("segment_id"))
                in ref_segments
            ]
            if matching and (not block_ids or block_ids[-1] != run["block_id"]):
                block_ids.append(run["block_id"])
    return {
        "kind": "resolved_selection",
        "resolution_hash": resolution_hash,
        "surface": "draft",
        "selection_kind": "source_excerpt",
        "display_range": {
            "anchor": _schema2_display_point(editor, anchor),
            "focus": _schema2_display_point(editor, focus),
        },
        "block_ids": list(dict.fromkeys(block_ids)),
        "refs": [_without_canonical(ref) for ref in refs if isinstance(ref, dict)],
        "canonical_text": resolution["canonical_text"],
        "degraded": resolution["degraded"],
    }


def test_schema2_drop_http_is_atomic_idempotent_and_exact(tmp_path: Path) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    blocks = _source_blocks()
    blocks.insert(
        1,
        {
            "block_id": "narration_one",
            "kind": "narration",
            "text": "旁白内容",
            "status": "draft",
            "recorded_refs": [],
        },
    )
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": blocks},
        )
        assert status == 201, created
        initial = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(initial, dict)
        candidate_id = initial["candidate"]["candidate_id"]  # type: ignore[index]
        source_paragraph = next(
            item for item in initial["paragraphs"]  # type: ignore[index]
            if any(
                isinstance(run, dict) and run.get("block_id") == "block_a"
                for run in item.get("source_runs", [])
            )
        )
        target_paragraph = next(
            item for item in initial["paragraphs"]  # type: ignore[index]
            if any(
                isinstance(run, dict) and run.get("block_id") == "block_b"
                for run in item.get("source_runs", [])
            )
        )
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": source_paragraph["paragraph_id"],
                    "offset": 0,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": source_paragraph["paragraph_id"],
                    "offset": 1,
                    "offset_encoding": "utf16",
                },
            },
        )
        assert status == 200, selection
        assert isinstance(selection, dict)
        source = _schema2_drop_source(initial, selection)
        target = {
            "paragraph_id": target_paragraph["paragraph_id"],
            "block_id": "block_b",
            "utf16_offset": 0,
        }
        body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": bool(selection["resolution"]["degraded"]),  # type: ignore[index]
                "source": source,
                "target": target,
            },
        )
        before_files = sorted((root / "content-drafts").glob("*.json"))
        before_generation = initial["workspace"]["expected_checkpoint_ref"]["generation"]  # type: ignore[index]
        status, inside = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                **body,
                "operation_id": f"dwop_{before_generation}_{secrets.token_hex(16)}",
                "target": {
                    "paragraph_id": source_paragraph["paragraph_id"],
                    "block_id": "block_a",
                    "utf16_offset": 0,
                },
            },
        )
        assert status == 400, inside
        assert inside["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert sorted((root / "content-drafts").glob("*.json")) == before_files
        unchanged = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert unchanged["candidate"]["candidate_id"] == candidate_id  # type: ignore[index]
        assert unchanged["workspace"] == initial["workspace"]  # type: ignore[index]
        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, moved
        _assert_edit_timing(moved, "move_selection")  # type: ignore[arg-type]
        child_id = moved["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert child_id != candidate_id
        assert moved["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == before_generation + 1  # type: ignore[index]
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1

        status, repeated = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, repeated
        assert repeated["draft_editor"]["candidate"] == moved["draft_editor"]["candidate"]  # type: ignore[index]
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1

        stale_body = {**body, "operation_id": f"dwop_{before_generation}_{secrets.token_hex(16)}"}
        status, stale = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=stale_body,
        )
        assert status == 409, stale
        assert stale["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1

        status, unknown = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={**body, "unknown": True},
        )
        assert status == 400, unknown
        assert unknown["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1

        current = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(current, dict)
        window_status, window = _request(
            review,
            "POST",
            "/api/workflow/draft-transcript-window",
            headers=_headers(review, write=True),
            body={"candidate_id": child_id, "source_id": "src_a", "offset": 0, "limit": 20},
        )
        assert window_status == 200, window
        assert isinstance(window, dict)
        source_window_paragraph = window["paragraphs"][0]  # type: ignore[index]
        source_status, source_selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": child_id,
                "surface": "source",
                "anchor": {"paragraph_id": source_window_paragraph["paragraph_id"], "offset": 0, "offset_encoding": "utf16"},
                "focus": {"paragraph_id": source_window_paragraph["paragraph_id"], "offset": 1, "offset_encoding": "utf16"},
            },
        )
        assert source_status == 200, source_selection
        assert isinstance(source_selection, dict)
        source_resolution = source_selection["resolution"]
        assert isinstance(source_resolution, dict)
        source_refs = source_resolution["refs"]
        assert isinstance(source_refs, list) and source_refs
        target = next(
            item for item in current["paragraphs"]  # type: ignore[index]
            if isinstance(item, dict) and "src_b" in str(item.get("text", ""))
        )
        exact_source = {
            "kind": "exact_source_refs",
            "source_id": source_refs[0]["source_id"],
            "transcript_version_id": source_refs[0]["transcript_version_id"],
            "refs": [_without_canonical(ref) for ref in source_refs],
            "canonical_text": source_resolution["canonical_text"],
        }
        insert_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "insert_source_refs",
                "accept_degraded": bool(source_resolution["degraded"]),
                "source": exact_source,
                "target": {
                    "paragraph_id": target["paragraph_id"],
                    "block_id": _paragraph_block_id(target),
                    "utf16_offset": 0,
                },
            },
        )
        status, inserted = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=insert_body,
        )
        assert status == 201, inserted
        _assert_edit_timing(inserted, "insert_source_refs")  # type: ignore[arg-type]
        inserted_id = inserted["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert inserted_id != child_id

        latest = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(latest, dict)
        narration_paragraph = next(
            item for item in latest["paragraphs"]  # type: ignore[index]
            if item.get("block_id") == "narration_one"
        )
        narration_status, narration_selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": inserted_id,
                "surface": "draft",
                "anchor": {"paragraph_id": narration_paragraph["paragraph_id"], "offset": 0, "offset_encoding": "utf16"},
                "focus": {"paragraph_id": narration_paragraph["paragraph_id"], "offset": 1, "offset_encoding": "utf16"},
            },
        )
        assert narration_status == 200, narration_selection
        assert isinstance(narration_selection, dict)
        narration_source = _schema2_drop_source(latest, narration_selection)
        target = next(
            item for item in latest["paragraphs"]  # type: ignore[index]
            if isinstance(item, dict) and "src_b" in str(item.get("text", ""))
        )
        narration_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": narration_source,
                "target": {
                    "paragraph_id": target["paragraph_id"],
                    "block_id": _paragraph_block_id(target),
                    "utf16_offset": 0,
                },
            },
        )
        status, narration_moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=narration_body,
        )
        assert status == 201, narration_moved
        assert narration_moved["draft_editor"]["candidate"]["candidate_id"] != inserted_id  # type: ignore[index]
        assert "result_highlight" not in narration_moved
        assert "result_selection" not in narration_moved


def test_schema2_punctuation_http_is_atomic_idempotent_and_stale_safe(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        source_paragraph = next(
            item
            for item in initial["paragraphs"]  # type: ignore[index]
            if any(
                isinstance(run, dict) and run.get("block_id") == "block_a"
                for run in item.get("source_runs", [])
            )
        )
        paragraph_id = source_paragraph["paragraph_id"]
        assert isinstance(paragraph_id, str)
        before_files = sorted((root / "content-drafts").glob("*.json"))
        before_workspace = initial["workspace"]
        assert isinstance(before_workspace, dict)
        before_generation = before_workspace["expected_checkpoint_ref"]["generation"]

        invalid = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "punctuation_edit",
                "payload": {
                    "paragraph_id": paragraph_id,
                    "block_id": "block_a",
                    "start_utf16_offset": 0,
                    "end_utf16_offset": 0,
                    "replacement": "字",
                },
            },
        )
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=invalid,
        )
        assert status == 400, rejected
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert sorted((root / "content-drafts").glob("*.json")) == before_files
        unchanged = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(unchanged, dict)
        assert unchanged["workspace"] == before_workspace

        body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "punctuation_edit",
                "payload": {
                    "paragraph_id": paragraph_id,
                    "block_id": "block_a",
                    "start_utf16_offset": 0,
                    "end_utf16_offset": 0,
                    "replacement": "“",
                },
            },
        )
        status, applied = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, applied
        _assert_edit_timing(applied, "punctuation_edit")  # type: ignore[arg-type]
        assert applied["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == before_generation + 1  # type: ignore[index]
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1

        assert applied["draft_editor"]["blocks"][1]["display_text"] == "“你好😀世界。"  # type: ignore[index]
        checkpoint = json.loads(
            (root / "workflow" / "draft-workspaces" / "wfr_review.json").read_text(
                encoding="utf-8"
            )
        )
        assert checkpoint["generation"] == before_generation + 1
        assert checkpoint["last_commit"]["operation"] == "punctuation_edit"

        status, repeated = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, repeated
        assert repeated["draft_editor"]["candidate"] == applied["draft_editor"]["candidate"]  # type: ignore[index]
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1

        stale = {**body, "operation_id": f"dwop_{before_generation}_{secrets.token_hex(16)}"}
        status, stale_response = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=stale,
        )
        assert status == 409, stale_response
        assert stale_response["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1

        status, unknown = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={**body, "unknown": True},
        )
        assert status == 400, unknown
        assert unknown["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1


def test_schema2_punctuation_http_boundary_ownership_and_cross_block_zero_write(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    blocks = [
        {
            "block_id": "block_boundary_left",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_emoji",
                    "start_ticks": 0,
                    "end_ticks": 120_000,
                }
            ],
            "canonical_text": "你好😀世界。",
        },
        {
            "block_id": "block_boundary_right",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_repeat_1",
                    "start_ticks": 130_000,
                    "end_ticks": 220_000,
                }
            ],
            "canonical_text": "重复。",
        },
    ]
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": blocks},
        )
        assert status == 201, created
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraph = next(
            item
            for item in initial["paragraphs"]  # type: ignore[index]
            if {run.get("block_id") for run in item.get("source_runs", []) if isinstance(run, dict)}
            >= {"block_boundary_left", "block_boundary_right"}
        )
        runs = [run for run in paragraph["source_runs"] if isinstance(run, dict)]
        left_run = next(run for run in runs if run["block_id"] == "block_boundary_left")
        right_run = next(run for run in runs if run["block_id"] == "block_boundary_right")
        boundary = int(left_run["end_offset"])
        assert boundary == int(right_run["start_offset"])
        boundary_utf16 = len(
            str(paragraph["text"])[:boundary].encode("utf-16-le")
        ) // 2
        before_generation = initial["workspace"]["expected_checkpoint_ref"]["generation"]  # type: ignore[index]

        status, inserted = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "schema_version": 2,
                    "operation": "punctuation_edit",
                    "payload": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "block_id": "block_boundary_right",
                        "start_utf16_offset": boundary_utf16,
                        "end_utf16_offset": boundary_utf16,
                        "replacement": "！",
                    },
                },
            ),
        )
        assert status == 201, inserted
        assert inserted["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == before_generation + 1  # type: ignore[index]

        status, after_insert = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, after_insert
        assert isinstance(after_insert, dict)
        inserted_block = next(
            block for block in after_insert["blocks"]  # type: ignore[index]
            if block.get("block_id") == "block_boundary_right"
        )
        assert inserted_block["display_text"] == "！重复。"  # type: ignore[index]
        after_insert_paragraph = next(
            item
            for item in after_insert["paragraphs"]  # type: ignore[index]
            if {run.get("block_id") for run in item.get("source_runs", []) if isinstance(run, dict)}
            >= {"block_boundary_left", "block_boundary_right"}
        )
        after_insert_runs = [
            run for run in after_insert_paragraph["source_runs"] if isinstance(run, dict)
        ]
        after_insert_left = next(
            run for run in after_insert_runs if run["block_id"] == "block_boundary_left"
        )
        after_insert_boundary = int(after_insert_left["end_offset"])
        after_insert_boundary_utf16 = len(
            str(after_insert_paragraph["text"])[:after_insert_boundary].encode("utf-16-le")
        ) // 2

        status, deleted_right = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "schema_version": 2,
                    "operation": "punctuation_edit",
                    "payload": {
                        "paragraph_id": after_insert_paragraph["paragraph_id"],
                        "block_id": "block_boundary_right",
                        "start_utf16_offset": after_insert_boundary_utf16,
                        "end_utf16_offset": after_insert_boundary_utf16 + 1,
                        "replacement": "",
                    },
                },
            ),
        )
        assert status == 201, deleted_right

        status, after_right_delete = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, after_right_delete
        assert isinstance(after_right_delete, dict)
        after_right_paragraph = next(
            item
            for item in after_right_delete["paragraphs"]  # type: ignore[index]
            if {run.get("block_id") for run in item.get("source_runs", []) if isinstance(run, dict)}
            >= {"block_boundary_left", "block_boundary_right"}
        )
        after_right_runs = [
            run for run in after_right_paragraph["source_runs"] if isinstance(run, dict)
        ]
        after_right_left = next(
            run for run in after_right_runs if run["block_id"] == "block_boundary_left"
        )
        after_right_boundary = int(after_right_left["end_offset"])
        after_right_boundary_utf16 = len(
            str(after_right_paragraph["text"])[:after_right_boundary].encode("utf-16-le")
        ) // 2

        status, deleted_left = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "schema_version": 2,
                    "operation": "punctuation_edit",
                    "payload": {
                        "paragraph_id": after_right_paragraph["paragraph_id"],
                        "block_id": "block_boundary_left",
                        "start_utf16_offset": after_right_boundary_utf16 - 1,
                        "end_utf16_offset": after_right_boundary_utf16,
                        "replacement": "",
                    },
                },
            ),
        )
        assert status == 201, deleted_left
        status, after_delete = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, after_delete
        assert isinstance(after_delete, dict)
        latest_paragraph = next(
            item
            for item in after_delete["paragraphs"]  # type: ignore[index]
            if {run.get("block_id") for run in item.get("source_runs", []) if isinstance(run, dict)}
            >= {"block_boundary_left", "block_boundary_right"}
        )
        latest_runs = [run for run in latest_paragraph["source_runs"] if isinstance(run, dict)]
        latest_left = next(run for run in latest_runs if run["block_id"] == "block_boundary_left")
        latest_right = next(run for run in latest_runs if run["block_id"] == "block_boundary_right")
        latest_boundary = int(latest_left["end_offset"])
        assert latest_boundary == int(latest_right["start_offset"])
        latest_boundary_utf16 = len(
            str(latest_paragraph["text"])[:latest_boundary].encode("utf-16-le")
        ) // 2
        before_failed_files = set((root / "content-drafts").glob("*.json"))
        before_failed_generation = after_delete["workspace"]["expected_checkpoint_ref"]["generation"]  # type: ignore[index]
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "schema_version": 2,
                    "operation": "punctuation_edit",
                    "payload": {
                        "paragraph_id": latest_paragraph["paragraph_id"],
                        "block_id": "block_boundary_left",
                        "start_utf16_offset": latest_boundary_utf16 - 1,
                        "end_utf16_offset": latest_boundary_utf16 + 1,
                        "replacement": "！",
                    },
                },
            ),
        )
        assert status == 400, rejected
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert set((root / "content-drafts").glob("*.json")) == before_failed_files
        status, unchanged = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, unchanged
        assert unchanged["workspace"]["expected_checkpoint_ref"]["generation"] == before_failed_generation  # type: ignore[index]


@pytest.mark.parametrize(
    ("case", "operation"),
    [("a", "move"), ("b", "delete"), ("b", "move")],
)
def test_schema2_real_selection_punctuation_http_is_atomic(
    tmp_path: Path,
    case: str,
    operation: str,
) -> None:
    fixture = _workflow_project(tmp_path, ownership_case=case, workflow_source_ids=("src_a",))
    root = Path(fixture["root"])
    specs = (
        [
            ("block_edit_p_aaaaaaaaaaaaaaaaaaaaaaaa_r_0000000000000001", "a01", 270_000, 300_000, "区。"),
            ("block_edit_p_aaaaaaaaaaaaaaaaaaaaaaaa_r_0000000000000002", "a02", 300_000, 422_727, "主持人说：“请大家"),
            ("block_edit_p_cccccccccccccccccccccccc_r_0000000000000003", "a03", 422_727, 500_000, "落点"),
        ]
        if case == "a"
        else [
            ("block_edit_p_bbbbbbbbbbbbbbbbbbbbbbbb_r_0000000000000001", "b01", 0, 280_000, "前文"),
            ("block_edit_p_bbbbbbbbbbbbbbbbbbbbbbbb_r_0000000000000002", "b02", 528_571, 600_000, "时间学习。"),
        ]
    )
    blocks = [
        {
            "block_id": block_id,
            "kind": "source_excerpt",
            "refs": [{"source_id": "src_a", "transcript_version_id": "tr_a", "segment_id": segment_id, "start_ticks": start, "end_ticks": end}],
            "canonical_text": text,
        }
        for block_id, segment_id, start, end, text in specs
    ]
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": blocks},
        )
        assert status == 201, created
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        assert isinstance(editor, dict)
        paragraph = next(
            item
            for item in editor["paragraphs"]
            if ("区。" if case == "a" else "时间学习。") in str(item["text"])
        )
        start = 0 if case == "a" else len("前文")
        end = (
            len("区。主持人说：“请大家")
            if case == "a"
            else start + len("时间学习")
        )
        selection = {
            "surface": "draft",
            "anchor": {
                "paragraph_id": paragraph["paragraph_id"],
                "offset": start,
                "offset_encoding": "utf16",
            },
            "focus": {
                "paragraph_id": paragraph["paragraph_id"],
                "offset": end,
                "offset_encoding": "utf16",
            },
        }
        candidate_id = editor["candidate"]["candidate_id"]
        status, resolved = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={"candidate_id": candidate_id, **selection},
        )
        assert status == 200, resolved
        assert [
            (ref["segment_id"], ref["start_ticks"], ref["end_ticks"])
            for ref in resolved["resolution"]["refs"]
        ] == (
            [("a01", 270_000, 285_000), ("a02", 300_000, 422_727)]
            if case == "a"
            else [("b02", 528_571, 585_714)]
        )
        body = {
            "candidate_id": candidate_id,
            "operation": operation,
            "selection": selection,
            "accept_degraded": False,
        }
        if operation == "move":
            target = (
                next(item for item in editor["paragraphs"] if item["text"] == "落点")
                if case == "a"
                else paragraph
            )
            body["caret"] = {
                "paragraph_id": target["paragraph_id"],
                "offset": len(str(target["text"])) if case == "a" else 0,
                "offset_encoding": "utf16",
            }
        before_files = set((root / "content-drafts").glob("*.json"))
        before_generation = editor["workspace"]["expected_checkpoint_ref"]["generation"]
        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(review, body),
        )
        assert status == 201, changed
        assert changed["operation"] == operation
        assert (
            changed["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"]
            == before_generation + 1
        )
        assert len(set((root / "content-drafts").glob("*.json")) - before_files) == 1
        checkpoint = json.loads(
            (root / "workflow" / "draft-workspaces" / "wfr_review.json").read_text(
                encoding="utf-8"
            )
        )
        assert checkpoint["generation"] == before_generation + 1
        assert checkpoint["last_commit"]["operation"] == f"edit_{operation}"
        final_blocks = [
            item
            for item in changed["draft_editor"]["blocks"]
            if item.get("kind") == "source_excerpt"
        ]
        refs = [ref for item in final_blocks for ref in item["refs"]]
        if case == "a":
            moved = next(
                item for item in final_blocks
                if item["refs"][0]["segment_id"] == "a01"
            )
            assert moved.get("display_text", moved["canonical_text"]) == "区。"
            assert not any(
                ref["segment_id"] == "a01" and ref["end_ticks"] > 285_000
                for ref in refs
            )
        else:
            owner = next(
                item for item in final_blocks
                if item["refs"][0]["segment_id"] == "b01"
            )
            assert owner["display_text"] == "前文。"
            assert not any(
                ref["segment_id"] == "b02" and ref["end_ticks"] > 585_714
                for ref in refs
            )


def test_draft_selection_resolve_exposes_adjusted_visible_range_without_changing_drop_range(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path, ownership_case="a", workflow_source_ids=("src_a",))
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={
                "blocks": [{
                    "block_id": "resolved_block",
                    "kind": "source_excerpt",
                    "refs": [{
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "a02",
                        "start_ticks": 300_000,
                        "end_ticks": 422_727,
                    }],
                    "canonical_text": "主持人说：“请大家",
                }],
            },
        )
        assert status == 201, created
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        assert isinstance(editor, dict)
        paragraph = editor["paragraphs"][0]
        selection = {
            "candidate_id": editor["candidate"]["candidate_id"],
            "surface": "draft",
            "anchor": {
                "paragraph_id": paragraph["paragraph_id"],
                "offset": 3,
                "offset_encoding": "utf16",
            },
            "focus": {
                "paragraph_id": paragraph["paragraph_id"],
                "offset": 5,
                "offset_encoding": "utf16",
            },
        }
        status, resolved = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body=selection,
        )
        assert status == 200, resolved
        assert resolved["display_range"]["anchor"]["utf16_offset"] == 3
        assert resolved["display_range"]["focus"]["utf16_offset"] == 5
        assert resolved["resolved_display_range"]["anchor"]["utf16_offset"] == 0
        assert resolved["resolved_display_range"]["focus"]["utf16_offset"] == 9
        assert resolved["resolution"]["canonical_text"] == "主持人说：“请大家"
        assert resolved["resolution"]["adjusted"] is True
        assert [
            (ref["start_ticks"], ref["end_ticks"])
            for ref in resolved["resolution"]["refs"]
        ] == [(300_000, 422_727)]
        source = _schema2_drop_source(editor, resolved)
        assert source["display_range"]["anchor"]["utf16_offset"] == 3
        assert source["display_range"]["focus"]["utf16_offset"] == 5


@pytest.mark.parametrize(
    ("operation", "expected_commit_operation"),
    [
        ("move_selection", "edit_move"),
        ("insert_source_refs", "edit_insert"),
        ("section_split", "section_split"),
    ],
)
def test_schema2_gap_boundary_http_operations_create_one_child(
    tmp_path: Path,
    operation: str,
    expected_commit_operation: str,
) -> None:
    fixture = _workflow_project(
        tmp_path,
        gap_boundary=True,
        workflow_source_ids=("src_a",),
    )
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _gap_schema2_blocks()},
        )
        assert status == 201, created
        initial = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(initial, dict)
        candidate_id = initial["candidate"]["candidate_id"]  # type: ignore[index]
        before_generation = initial["workspace"]["expected_checkpoint_ref"][  # type: ignore[index]
            "generation"
        ]
        paragraph = next(
            item
            for item in initial["paragraphs"]  # type: ignore[index]
            if any(
                isinstance(run, dict) and run.get("block_id") == "gap_block"
                for run in item.get("source_runs", [])
            )
        )

        if operation == "move_selection":
            selection_status, selection = _request(
                review,
                "POST",
                "/api/workflow/draft-selection-resolve",
                headers=_headers(review, write=True),
                body={
                    "candidate_id": candidate_id,
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 2,
                        "offset_encoding": "utf16",
                    },
                    "focus": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 3,
                        "offset_encoding": "utf16",
                    },
                },
            )
            assert selection_status == 200, selection
            assert isinstance(selection, dict)
            resolution = selection["resolution"]
            assert isinstance(resolution, dict)
            assert resolution["canonical_text"] == "乙 "
            source = _schema2_drop_source(initial, selection)
            request_body = {
                "schema_version": 2,
                "operation": operation,
                "accept_degraded": False,
                "source": source,
                "target": {
                    "paragraph_id": paragraph["paragraph_id"],
                    "block_id": "gap_block",
                    "utf16_offset": 0,
                },
            }
            expected_evidence = [
                (250, 350, "乙 "),
                (100, 200, "甲，"),
                (380, 480, "丙。"),
            ]
        elif operation == "insert_source_refs":
            window_status, window = _request(
                review,
                "POST",
                "/api/workflow/draft-transcript-window",
                headers=_headers(review, write=True),
                body={
                    "candidate_id": candidate_id,
                    "source_id": "src_a",
                    "offset": 0,
                    "limit": 20,
                },
            )
            assert window_status == 200, window
            assert isinstance(window, dict)
            source_paragraph = window["paragraphs"][0]  # type: ignore[index]
            selection_status, selection = _request(
                review,
                "POST",
                "/api/workflow/draft-selection-resolve",
                headers=_headers(review, write=True),
                body={
                    "candidate_id": candidate_id,
                    "surface": "source",
                    "anchor": {
                        "paragraph_id": source_paragraph["paragraph_id"],
                        "offset": 0,
                        "offset_encoding": "utf16",
                    },
                    "focus": {
                        "paragraph_id": source_paragraph["paragraph_id"],
                        "offset": 2,
                        "offset_encoding": "utf16",
                    },
                },
            )
            assert selection_status == 200, selection
            assert isinstance(selection, dict)
            resolution = selection["resolution"]
            assert isinstance(resolution, dict)
            refs = resolution["refs"]
            assert isinstance(refs, list) and refs
            assert resolution["canonical_text"] == "甲，"
            source = {
                "kind": "exact_source_refs",
                "source_id": refs[0]["source_id"],
                "transcript_version_id": refs[0]["transcript_version_id"],
                "refs": [_without_canonical(ref) for ref in refs],
                "canonical_text": resolution["canonical_text"],
            }
            request_body = {
                "schema_version": 2,
                "operation": operation,
                "accept_degraded": False,
                "source": source,
                "target": {
                    "paragraph_id": paragraph["paragraph_id"],
                    "block_id": "gap_block",
                    "utf16_offset": 2,
                },
            }
            expected_evidence = [
                (100, 200, "甲，"),
                (100, 200, "甲，"),
                (250, 500, "乙 丙。"),
            ]
        else:
            request_body = {
                "schema_version": 2,
                "operation": operation,
                "payload": {
                    "heading_block_id": "gap_heading",
                    "target": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "block_id": "gap_block",
                        "utf16_offset": 2,
                    },
                    "title": "新章节",
                },
            }
            expected_evidence = [
                (100, 200, "甲，"),
                (250, 500, "乙 丙。"),
            ]

        body = _workspace_body(review, request_body)
        operation_id = body["operation_id"]
        assert isinstance(operation_id, str)
        before_files = sorted((root / "content-drafts").glob("*.json"))
        response_status, response = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert response_status == 201, response
        assert isinstance(response, dict)
        edited = response["draft_editor"]
        assert isinstance(edited, dict)
        assert edited["candidate"]["candidate_id"] != candidate_id  # type: ignore[index]
        assert edited["workspace"]["expected_checkpoint_ref"]["generation"] == (  # type: ignore[index]
            before_generation + 1
        )
        after_files = sorted((root / "content-drafts").glob("*.json"))
        assert len(after_files) == len(before_files) + 1

        evidence = [
            (
                block["refs"][0]["start_ticks"],
                block["refs"][0]["end_ticks"],
                block["canonical_text"],
            )
            for block in edited["blocks"]  # type: ignore[index]
            if isinstance(block, dict) and block.get("kind") == "source_excerpt"
        ]
        assert evidence == expected_evidence
        assert all(end <= 200 or start >= 250 for start, end, _text in evidence)
        assert "".join(text for _start, _end, text in evidence) == {
            "move_selection": "乙 甲，丙。",
            "insert_source_refs": "甲，甲，乙 丙。",
            "section_split": "甲，乙 丙。",
        }[operation]

        if operation in {"move_selection", "insert_source_refs"}:
            result_selection = response["result_selection"]
            assert isinstance(result_selection, dict)
            assert result_selection["surface"] == "draft"
            assert result_selection["accepted_degraded"] is True
            request = result_selection["request"]
            assert isinstance(request, dict)
            rs_response = result_selection["response"]
            assert isinstance(rs_response, dict)
            assert rs_response["candidate_id"] == edited["candidate"]["candidate_id"]
            assert rs_response["surface"] == "draft"
            rs_resolution = rs_response["resolution"]
            assert isinstance(rs_resolution, dict)
            assert rs_resolution["direction"] == "forward"
            assert rs_resolution["adjusted"] is False
            assert rs_resolution["degraded"] is False
            rs_display = rs_response["display_range"]
            rs_resolved = rs_response["resolved_display_range"]
            assert isinstance(rs_display, dict) and isinstance(rs_resolved, dict)
            assert rs_display == rs_resolved
            highlight_start = 0 if operation == "move_selection" else 2
            assert rs_display["anchor"]["character_offset"] == highlight_start
            assert rs_display["focus"]["character_offset"] == highlight_start + 2
            assert (
                rs_display["anchor"]["paragraph_id"]
                == edited["paragraphs"][0]["paragraph_id"]
            )
            assert request["anchor"]["paragraph_id"] == rs_resolved["anchor"]["paragraph_id"]
            assert request["anchor"]["offset"] == rs_resolved["anchor"]["utf16_offset"]
            assert request["anchor"]["offset_encoding"] == "utf16"
            assert request["focus"]["offset"] == rs_resolved["focus"]["utf16_offset"]
            start_caret = rs_resolution["start_caret"]
            end_caret = rs_resolution["end_caret"]
            assert isinstance(start_caret, dict) and isinstance(end_caret, dict)
            assert start_caret["paragraph_id"] != rs_display["anchor"]["paragraph_id"]
            assert start_caret["paragraph_id"] == end_caret["paragraph_id"]
            correspondence = rs_response["correspondence_groups"]
            assert isinstance(correspondence, list) and correspondence
            assert isinstance(rs_response["resolution_hash"], str)
            assert len(rs_response["resolution_hash"]) == 64

            readback_status, readback = _request(
                review,
                "POST",
                "/api/workflow/draft-edit",
                headers=_headers(review, write=True),
                body=body,
            )
            assert readback_status == 201, readback
            assert "result_highlight" not in readback
            assert "result_selection" not in readback
            assert readback["draft_editor"]["candidate"] == edited["candidate"]
            assert readback["draft_editor"]["workspace"] == edited["workspace"]
            assert sorted((root / "content-drafts").glob("*.json")) == after_files
            assert json.loads(
                (root / "workflow" / "draft-workspaces" / "wfr_review.json").read_text(
                    encoding="utf-8"
                )
            )["generation"] == before_generation + 1
        else:
            assert "result_highlight" not in response
            assert "result_selection" not in response

        checkpoint = json.loads(
            (root / "workflow" / "draft-workspaces" / "wfr_review.json").read_text(
                encoding="utf-8"
            )
        )
        assert checkpoint["generation"] == before_generation + 1
        assert checkpoint["last_commit"]["operation"] == expected_commit_operation
        assert checkpoint["last_commit"]["operation_id"] == operation_id

        if operation == "move_selection":
            stale_body = {
                **body,
                "operation_id": f"dwop_{before_generation}_{secrets.token_hex(16)}",
            }
            stale_status, stale = _request(
                review,
                "POST",
                "/api/workflow/draft-edit",
                headers=_headers(review, write=True),
                body=stale_body,
            )
            assert stale_status == 409, stale
            assert stale["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
            assert sorted((root / "content-drafts").glob("*.json")) == after_files


def test_schema2_generated_paragraph_boundary_split_is_atomic_and_visible(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={
                "blocks": [
                    {
                        "block_id": "people_section",
                        "kind": "section_title",
                        "title": "人物章节",
                    },
                    _source_blocks()[1],
                    _source_blocks()[0],
                ]
            },
        )
        assert status == 201, created
        initial = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(initial, dict)
        source = next(
            item
            for item in initial["paragraphs"]  # type: ignore[index]
            if "你好😀世界。" in str(item.get("text", ""))
        )
        target = next(
            item
            for item in initial["paragraphs"]  # type: ignore[index]
            if "src_b 内容。" in str(item.get("text", ""))
        )
        selection_status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": initial["candidate"]["candidate_id"],  # type: ignore[index]
                "surface": "draft",
                "anchor": {
                    "paragraph_id": source["paragraph_id"],
                    "offset": 2,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": source["paragraph_id"],
                    "offset": 4,
                    "offset_encoding": "utf16",
                },
            },
        )
        assert selection_status == 200, selection
        assert isinstance(selection, dict)
        move_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": _schema2_drop_source(initial, selection),
                "target": {
                    "paragraph_id": target["paragraph_id"],
                    "block_id": "block_b",
                    "utf16_offset": 0,
                },
            },
        )
        move_status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=move_body,
        )
        assert move_status == 201, moved
        assert isinstance(moved, dict)
        moved_editor = moved["draft_editor"]
        assert isinstance(moved_editor, dict)
        mixed = next(
            item
            for item in moved_editor["paragraphs"]  # type: ignore[index]
            if "😀src_b 内容。" in str(item.get("text", ""))
        )
        runs = mixed["source_runs"]
        assert isinstance(runs, list) and [run["text"] for run in runs] == [
            "😀",
            "src_b 内容。",
        ]
        old_tokens = [str(run["block_id"]).split("_")[3] for run in runs]
        assert old_tokens[0] == old_tokens[1]

        before_files = sorted((root / "content-drafts").glob("*.json"))
        before_generation = moved_editor["workspace"]["expected_checkpoint_ref"][  # type: ignore[index]
            "generation"
        ]
        split_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "section_split",
                "payload": {
                    "heading_block_id": "people_section",
                    "target": {
                        "paragraph_id": mixed["paragraph_id"],
                        "block_id": runs[1]["block_id"],
                        "utf16_offset": 2,
                    },
                    "title": "边界新章",
                },
            },
        )
        operation_id = split_body["operation_id"]
        split_status, split = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=split_body,
        )
        assert split_status == 201, split
        assert isinstance(split, dict)
        _assert_edit_timing(split, "section_split")
        editor = split["draft_editor"]
        assert isinstance(editor, dict)
        assert editor["candidate"]["parent_candidate_id"] == moved_editor["candidate"][  # type: ignore[index]
            "candidate_id"
        ]
        assert editor["workspace"]["expected_checkpoint_ref"]["generation"] == (  # type: ignore[index]
            before_generation + 1
        )
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1
        paragraphs = [
            item
            for item in editor["paragraphs"]  # type: ignore[index]
            if item.get("kind") == "source_excerpt"
        ]
        assert [item["text"] for item in paragraphs] == [
            "你好世界。",
            "😀",
            "src_b 内容。",
        ]
        assert [item["section_title"] for item in paragraphs] == [
            "人物章节",
            None,
            "边界新章",
        ]
        headings = [
            block
            for block in editor["blocks"]  # type: ignore[index]
            if block.get("kind") == "section_title"
        ]
        assert [block["title"] for block in headings] == ["人物章节", "边界新章"]
        assert len({block["block_id"] for block in headings}) == 2
        right = next(item for item in paragraphs if item["text"] == "src_b 内容。")
        right_token = str(right["source_runs"][0]["block_id"]).split("_")[3]
        assert right_token != old_tokens[0]

        checkpoint = json.loads(
            (root / "workflow" / "draft-workspaces" / "wfr_review.json").read_text(
                encoding="utf-8"
            )
        )
        assert checkpoint["generation"] == before_generation + 1
        assert checkpoint["last_commit"]["operation"] == "section_split"
        assert checkpoint["last_commit"]["operation_id"] == operation_id
        latest = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert latest["candidate"] == editor["candidate"]  # type: ignore[index]


def test_schema2_generated_paragraph_internal_split_is_atomic(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path, compound=True)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={
                "blocks": [
                    {
                        "block_id": "source_section",
                        "kind": "section_title",
                        "title": "来源章节",
                    },
                    {
                        "block_id": "source_a0",
                        "kind": "source_excerpt",
                        "refs": [{
                            "source_id": "src_a",
                            "transcript_version_id": "tr_a",
                            "segment_id": "seg_a_0",
                            "start_ticks": 0,
                            "end_ticks": 40_000,
                        }],
                        "canonical_text": "A0",
                    },
                    {
                        "block_id": "target_section",
                        "kind": "section_title",
                        "title": "目标章节",
                    },
                    {
                        "block_id": "target_b0",
                        "kind": "source_excerpt",
                        "refs": [{
                            "source_id": "src_b",
                            "transcript_version_id": "tr_b",
                            "segment_id": "seg_b_0",
                            "start_ticks": 0,
                            "end_ticks": 40_000,
                        }],
                        "canonical_text": "B0",
                    },
                    {
                        "block_id": "target_b1",
                        "kind": "source_excerpt",
                        "refs": [{
                            "source_id": "src_b",
                            "transcript_version_id": "tr_b",
                            "segment_id": "seg_b_1",
                            "start_ticks": 60_000,
                            "end_ticks": 100_000,
                        }],
                        "canonical_text": "B1",
                    },
                ]
            },
        )
        assert status == 201, created
        initial = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(initial, dict)
        source = next(
            item
            for item in initial["paragraphs"]  # type: ignore[index]
            if item.get("text") == "A0"
        )
        target = next(
            item
            for item in initial["paragraphs"]  # type: ignore[index]
            if item.get("text") == "B0B1"
        )
        selection_status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": initial["candidate"]["candidate_id"],  # type: ignore[index]
                "surface": "draft",
                "anchor": {
                    "paragraph_id": source["paragraph_id"],
                    "offset": 0,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": source["paragraph_id"],
                    "offset": 2,
                    "offset_encoding": "utf16",
                },
            },
        )
        assert selection_status == 200, selection
        move_status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "schema_version": 2,
                    "operation": "move_selection",
                    "accept_degraded": False,
                    "source": _schema2_drop_source(initial, selection),
                    "target": {
                        "paragraph_id": target["paragraph_id"],
                        "block_id": "target_b0",
                        "utf16_offset": 0,
                    },
                },
            ),
        )
        assert move_status == 201, moved
        assert isinstance(moved, dict)
        moved_editor = moved["draft_editor"]
        mixed = next(
            item
            for item in moved_editor["paragraphs"]  # type: ignore[index]
            if item.get("text") == "A0B0B1"
        )
        runs = mixed["source_runs"]
        assert isinstance(runs, list) and [run["text"] for run in runs] == [
            "A0",
            "B0",
            "B1",
        ]
        old_tokens = [str(run["block_id"]).split("_")[3] for run in runs]
        assert len(set(old_tokens)) == 1

        before_files = sorted((root / "content-drafts").glob("*.json"))
        before_generation = moved_editor["workspace"]["expected_checkpoint_ref"][  # type: ignore[index]
            "generation"
        ]
        split_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "section_split",
                "payload": {
                    "heading_block_id": "target_section",
                    "target": {
                        "paragraph_id": mixed["paragraph_id"],
                        "block_id": runs[1]["block_id"],
                        "utf16_offset": 3,
                    },
                    "title": "段内新章",
                },
            },
        )
        operation_id = split_body["operation_id"]
        split_status, split = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=split_body,
        )
        assert split_status == 201, split
        assert isinstance(split, dict)
        _assert_edit_timing(split, "section_split")
        editor = split["draft_editor"]
        assert editor["candidate"]["parent_candidate_id"] == moved_editor["candidate"][  # type: ignore[index]
            "candidate_id"
        ]
        assert editor["workspace"]["expected_checkpoint_ref"]["generation"] == (  # type: ignore[index]
            before_generation + 1
        )
        assert len(sorted((root / "content-drafts").glob("*.json"))) == len(before_files) + 1
        paragraphs = [
            item
            for item in editor["paragraphs"]  # type: ignore[index]
            if item.get("kind") == "source_excerpt"
        ]
        assert [item["text"] for item in paragraphs] == ["A0B", "0B1"]
        assert [item["section_title"] for item in paragraphs] == [
            "目标章节",
            "段内新章",
        ]
        blocks = editor["blocks"]
        heading_index = next(
            index
            for index, block in enumerate(blocks)
            if block.get("kind") == "section_title" and block.get("title") == "段内新章"
        )
        assert blocks[heading_index - 2]["block_id"].split("_")[3] == old_tokens[0]
        assert blocks[heading_index - 1]["block_id"].split("_")[3] == old_tokens[0]
        right_token = blocks[heading_index + 1]["block_id"].split("_")[3]
        assert right_token != old_tokens[0]
        assert blocks[heading_index + 2]["block_id"].split("_")[3] == right_token

        checkpoint = json.loads(
            (root / "workflow" / "draft-workspaces" / "wfr_review.json").read_text(
                encoding="utf-8"
            )
        )
        assert checkpoint["generation"] == before_generation + 1
        assert checkpoint["last_commit"]["operation"] == "section_split"
        assert checkpoint["last_commit"]["operation_id"] == operation_id
        latest = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert latest["candidate"] == editor["candidate"]  # type: ignore[index]


def test_schema2_section_http_operations_use_closed_payloads(tmp_path: Path) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review, "POST", "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _schema2_section_blocks()},
        )
        assert status == 201, created
        snapshot = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(snapshot, dict)
        initial_id = snapshot["candidate"]["candidate_id"]  # type: ignore[index]

        def section(operation: str, payload: dict[str, object]) -> dict[str, object]:
            nonlocal snapshot
            before_generation = snapshot["workspace"]["expected_checkpoint_ref"]["generation"]  # type: ignore[index]
            status, response = _request(
                review,
                "POST",
                "/api/workflow/draft-edit",
                headers=_headers(review, write=True),
                body=_workspace_body(
                    review,
                    {"schema_version": 2, "operation": operation, "payload": payload},
                ),
            )
            assert status == 201, response
            assert isinstance(response, dict)
            _assert_edit_timing(response, operation)
            next_snapshot = response["draft_editor"]
            assert isinstance(next_snapshot, dict)
            assert next_snapshot["workspace"]["expected_checkpoint_ref"]["generation"] == before_generation + 1  # type: ignore[index]
            assert next_snapshot["candidate"]["candidate_id"] != snapshot["candidate"]["candidate_id"]  # type: ignore[index]
            snapshot = next_snapshot
            return next_snapshot

        section("section_rename", {"heading_block_id": "heading_one", "title": "改名章节"})
        section("section_reorder", {"heading_block_id": "heading_two", "before_heading_block_id": "heading_one"})

        split_paragraph = next(
            item for item in snapshot["paragraphs"]  # type: ignore[index]
            if any(
                isinstance(run, dict) and run.get("block_id") == "block_a"
                for run in item.get("source_runs", [])
            )
        )
        split_snapshot = section(
            "section_split",
            {
                "heading_block_id": "heading_two",
                "target": {
                    "paragraph_id": split_paragraph["paragraph_id"],
                    "block_id": "block_a",
                    "utf16_offset": 1,
                },
                "title": "拆分章节",
            },
        )
        headings = [
            block["block_id"]
            for block in split_snapshot["blocks"]  # type: ignore[index]
            if block.get("kind") == "section_title"
        ]
        generated = next(heading for heading in headings if heading not in {"heading_one", "heading_two"})
        section("section_merge", {"heading_block_id": generated, "direction": "previous", "adjacent_heading_block_id": "heading_two"})
        section("section_delete", {"heading_block_id": "heading_one"})

        before_invalid = sorted((root / "content-drafts").glob("*.json"))
        status, invalid = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "schema_version": 2,
                    "operation": "section_rename",
                    "payload": {"heading_block_id": "heading_two", "title": "非法", "unknown": True},
                },
            ),
        )
        assert status == 400, invalid
        assert invalid["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert sorted((root / "content-drafts").glob("*.json")) == before_invalid
        assert initial_id not in {
            snapshot["candidate"]["candidate_id"]  # type: ignore[index]
        }


def _compound_source_blocks() -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for source_id, transcript_id in (("src_b", "tr_b"), ("src_a", "tr_a")):
        blocks.append(
            {
                "block_id": f"block_{source_id}",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": source_id,
                        "transcript_version_id": transcript_id,
                        "segment_id": f"seg_{source_id[-1]}_{index}",
                        "start_ticks": index * 60_000,
                        "end_ticks": index * 60_000 + 40_000,
                    }
                    for index in range(6)
                ],
                "canonical_text": "\n".join(
                    f"{source_id[-1].upper()}{index}" for index in range(6)
                ),
            }
        )
    blocks.append(
        {
            "block_id": "block_tail",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_tail",
                    "start_ticks": 480_000,
                    "end_ticks": 540_000,
                }
            ],
            "canonical_text": "收尾段",
        }
    )
    return blocks


def _seed_workflow_draft(
    root: Path,
    *,
    bindings: list[dict[str, object]],
    blocks: list[dict[str, object]],
) -> str:
    status = workflow_status(root)
    run = status["workflow_run"]
    assert isinstance(run, dict)
    run_id = run["run_id"]
    refs = run["artifact_refs"]
    assert isinstance(run_id, str) and isinstance(refs, dict)
    brief_ref = refs["brief"]
    assert isinstance(brief_ref, dict)
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=bindings,
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    result = workflow_action(
        root,
        run_id,
        "act_review_seed_" + secrets.token_hex(10),
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": None,
            "source_bindings": bindings,
            "brief_ref": brief_ref,
            "context_hash": context.context_hash,
            "blocks": blocks,
            "scoped_mutable_block_ids": [],
        },
    )
    ref = result.workflow_run.artifact_refs["content_draft"]
    assert ref is not None
    return ref.artifact_id


def _create_confirm_propose(review) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    status, created = _request(
        review,
        "POST",
        "/api/workflow/content-drafts",
        headers=_headers(review, write=True),
        body={"blocks": _source_blocks()},
    )
    assert status == 201, created
    candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
        "content_draft_id"
    ]
    candidate_revision = created["content_draft_mutation"]["project_revision"]  # type: ignore[index]
    assert candidate_revision == created["workflow"]["project"]["revision"]  # type: ignore[index]
    status, candidate = _request(review, "GET", "/api/workflow/content-draft")
    assert status == 200
    assert candidate["content_draft"]["content_draft_id"] == candidate_id  # type: ignore[index]
    assert candidate["status"] == "current"  # type: ignore[index]
    assert "content_draft_confirm" in created["workflow"]["allowed_operations"]  # type: ignore[index]
    assert "content_draft_propose" not in created["workflow"]["allowed_operations"]  # type: ignore[index]

    status, confirmed = _request(
        review,
        "POST",
        "/api/workflow/content-draft-confirm",
        headers=_headers(review, write=True),
        body={"content_draft_id": candidate_id},
    )
    assert status == 200, confirmed
    confirmed_id = confirmed["content_draft_mutation"]["content_draft"][  # type: ignore[index]
        "content_draft_id"
    ]
    assert confirmed_id != candidate_id
    assert confirmed["content_draft_mutation"]["project_revision"] == candidate_revision + 1  # type: ignore[index]
    assert confirmed["workflow"]["content_draft"]["content_draft"][  # type: ignore[index]
        "confirmed_by_user"
    ] is True
    assert "content_draft_propose" in confirmed["workflow"]["allowed_operations"]  # type: ignore[index]
    assert "content_draft_confirm" not in confirmed["workflow"]["allowed_operations"]  # type: ignore[index]
    status, repeated = _request(
        review,
        "POST",
        "/api/workflow/content-draft-confirm",
        headers=_headers(review, write=True),
        body={"content_draft_id": candidate_id},
    )
    assert status == 200, repeated
    assert repeated["workflow_receipt"] == confirmed["workflow_receipt"]  # type: ignore[index]
    assert repeated["content_draft_mutation"] == confirmed["content_draft_mutation"]  # type: ignore[index]

    status, proposed = _request(
        review,
        "POST",
        "/api/workflow/content-draft-propose",
        headers=_headers(review, write=True),
        body={"content_draft_id": confirmed_id},
    )
    assert status == 200, proposed
    proposal = proposed["content_draft_proposal"]["proposal"]  # type: ignore[index]
    assert [clip["source_id"] for clip in proposal["clips"]] == ["src_b", "src_a"]
    assert proposed["review"]["basis"] == {  # type: ignore[index]
        "type": "proposal",
        "id": proposal["proposal_id"],
    }
    return confirmed_id, proposal["proposal_id"]


def test_draft_editor_api_snapshot_exact_edit_search_and_session_history(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    initial_revision = ProjectStore(root).load().revision
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]

        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        assert editor["review_mode"] == "draft_editor"  # type: ignore[index]
        assert editor["candidate"]["candidate_id"] == candidate_id  # type: ignore[index]
        assert editor["history"] == {  # type: ignore[index]
            "can_undo": False,
            "can_redo": False,
            "redo_scope": "draft_workspace",
        }
        assert [item["source_id"] for item in editor["sources"]] == [  # type: ignore[index]
            "src_b",
            "src_a",
        ]
        serialized = json.dumps(editor, ensure_ascii=False)
        assert str(root) not in serialized
        assert str(tmp_path) not in serialized
        assert review.token not in serialized
        assert "context_hash" not in serialized
        assert "revision" not in serialized.lower()
        assert all(
            {
                "start_offset",
                "end_offset",
                "source_start_offset",
                "source_end_offset",
            }.issubset(run)
            for paragraph in editor["paragraphs"]  # type: ignore[index]
            for run in paragraph["source_runs"]
        )
        assert not (root / "proposals").exists()
        assert not (root / "edits").exists()

        draft_paragraph = next(
            item
            for item in editor["paragraphs"]  # type: ignore[index]
            if "你好😀世界。" in item["text"]
        )
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": draft_paragraph["paragraph_id"],
                    "offset": 4,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": draft_paragraph["paragraph_id"],
                    "offset": 1,
                    "offset_encoding": "codepoint",
                },
            },
        )
        assert status == 200, selection
        assert selection["resolution"]["direction"] == "backward"  # type: ignore[index]
        assert selection["resolution"]["canonical_text"] == "好😀"  # type: ignore[index]
        assert selection["display_range"] == {  # type: ignore[index]
            "anchor": {
                "paragraph_id": draft_paragraph["paragraph_id"],
                "character_offset": 3,
                "utf16_offset": 4,
            },
            "focus": {
                "paragraph_id": draft_paragraph["paragraph_id"],
                "character_offset": 1,
                "utf16_offset": 1,
            },
        }
        assert "view_hash" not in json.dumps(selection)

        status, caret = _request(
            review,
            "POST",
            "/api/workflow/draft-caret-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "paragraph_id": draft_paragraph["paragraph_id"],
                "offset": 6,
                "offset_encoding": "utf16",
            },
        )
        assert status == 200, caret
        assert caret["candidate_id"] == candidate_id  # type: ignore[index]
        assert "boundary_id" in caret

        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": draft_paragraph["paragraph_id"],
                        "offset": 1,
                        "offset_encoding": "utf16",
                    },
                    "focus": {
                        "paragraph_id": draft_paragraph["paragraph_id"],
                        "offset": 5,
                        "offset_encoding": "utf16",
                    },
                },
                "accept_degraded": False,
            },
        )
        assert status == 201, changed
        child_id = changed["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert child_id != candidate_id
        assert changed["draft_editor"]["candidate"]["parent_candidate_id"] == candidate_id  # type: ignore[index]
        assert "好😀世" not in "".join(
            item["text"] for item in changed["draft_editor"]["paragraphs"]  # type: ignore[index]
        )
        assert ProjectStore(root).load().revision == initial_revision
        assert (root / "content-drafts" / f"{candidate_id}.json").is_file()
        assert (root / "content-drafts" / f"{child_id}.json").is_file()

        status, stale = _request(
            review,
            "POST",
            "/api/workflow/draft-search",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "surface": "source",
                "query": "重复",
                "offset": 0,
                "limit": 20,
            },
        )
        assert status == 400
        assert stale["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]

        status, searched = _request(
            review,
            "POST",
            "/api/workflow/draft-search",
            headers=_headers(review, write=True),
            body={
                "candidate_id": child_id,
                "surface": "source",
                "query": "重复",
                "offset": 0,
                "limit": 20,
            },
        )
        assert status == 200, searched
        assert searched["total"] == 2  # type: ignore[index]
        assert len({item["match_id"] for item in searched["matches"]}) == 2  # type: ignore[index]

        status, undone = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": child_id},
        )
        assert status == 200, undone
        assert undone["draft_editor"]["candidate"]["candidate_id"] == candidate_id  # type: ignore[index]
        assert undone["draft_editor"]["history"]["can_redo"] is True  # type: ignore[index]

        status, redone = _request(
            review,
            "POST",
            "/api/workflow/draft-redo",
            headers=_headers(review, write=True),
            body={"candidate_id": candidate_id},
        )
        assert status == 200, redone
        assert redone["draft_editor"]["candidate"]["candidate_id"] == child_id  # type: ignore[index]
        assert redone["draft_editor"]["history"]["can_redo"] is False  # type: ignore[index]
        assert ProjectStore(root).load().revision == initial_revision

        status, undone_again = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": child_id},
        )
        assert status == 200, undone_again
        original_editor = undone_again["draft_editor"]  # type: ignore[index]
        original_paragraph = next(
            item
            for item in original_editor["paragraphs"]
            if "你好😀世界。" in item["text"]
        )
        status, branched = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": original_paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": original_paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "accept_degraded": False,
            },
        )
        assert status == 201, branched
        branch_id = branched["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert branch_id not in {candidate_id, child_id}
        assert branched["draft_editor"]["history"]["can_redo"] is False  # type: ignore[index]
        status, no_redo = _request(
            review,
            "POST",
            "/api/workflow/draft-redo",
            headers=_headers(review, write=True),
            body={"candidate_id": branch_id},
        )
        assert status == 409
        assert no_redo["error"]["code"] == "draft_workspace_transition_not_allowed"  # type: ignore[index]
        assert (root / "content-drafts" / f"{child_id}.json").is_file()
        assert ProjectStore(root).load().revision == initial_revision


def test_draft_workspace_http_restart_recovers_current_redo_and_two_services_cas(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    bindings = fixture["bindings"]
    revision = ProjectStore(root).load().revision
    static = _static(tmp_path)

    with start_review_server(
        root,
        source_bindings=bindings,
        static_root=static,
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        initial = _request(review, "GET", "/api/workflow/draft-editor")[1]
        parent_id = initial["candidate"]["candidate_id"]  # type: ignore[index]
        paragraph = initial["paragraphs"][0]  # type: ignore[index]
        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": parent_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "accept_degraded": True,
            },
        )
        assert status == 201, changed
        child_id = changed["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        status, undone = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": child_id},
        )
        assert status == 200, undone
        assert undone["draft_editor"]["history"]["can_redo"] is True  # type: ignore[index]

    with (
        start_review_server(
            root,
            source_bindings=bindings,
            static_root=static,
        ) as first,
        start_review_server(
            root,
            source_bindings=bindings,
            static_root=static,
        ) as second,
    ):
        first_status, first_snapshot = _request(
            first, "GET", "/api/workflow/draft-editor"
        )
        second_status, second_snapshot = _request(
            second, "GET", "/api/workflow/draft-editor"
        )
        assert first_status == 200, first_snapshot
        assert second_status == 200, second_snapshot
        assert first_snapshot["candidate"]["candidate_id"] == parent_id  # type: ignore[index]
        assert first_snapshot["history"]["can_redo"] is True  # type: ignore[index]
        assert second_snapshot["workspace"] == first_snapshot["workspace"]  # type: ignore[index]
        paragraph = first_snapshot["paragraphs"][0]  # type: ignore[index]
        business = {
            "candidate_id": parent_id,
            "operation": "delete",
            "selection": {
                "surface": "draft",
                "anchor": {
                    "paragraph_id": paragraph["paragraph_id"],
                    "offset": 0,
                },
                "focus": {
                    "paragraph_id": paragraph["paragraph_id"],
                    "offset": 1,
                },
            },
            "accept_degraded": True,
        }
        first_body = _workspace_body(first, business)
        second_body = {
            **business,
            "operation_id": (
                f"dwop_{second_snapshot['workspace']['expected_checkpoint_ref']['generation']}"  # type: ignore[index]
                f"_{secrets.token_hex(16)}"
            ),
            "expected_checkpoint_ref": second_snapshot["workspace"][  # type: ignore[index]
                "expected_checkpoint_ref"
            ],
            "expected_current_candidate_ref": second_snapshot["workspace"][  # type: ignore[index]
                "expected_current_candidate_ref"
            ],
        }
        assert _request(
            first,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(first, write=True),
            body=first_body,
        )[0] == 201
        status, stale = _request(
            second,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(second, write=True),
            body=second_body,
        )
        assert status == 409, stale
        assert stale["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
        assert ProjectStore(root).load().revision == revision


def test_draft_workspace_http_idempotency_conflict_and_required_envelope(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        assert _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )[0] == 201
        snapshot = _request(review, "GET", "/api/workflow/draft-editor")[1]
        candidate_id = snapshot["candidate"]["candidate_id"]  # type: ignore[index]
        paragraph = snapshot["paragraphs"][0]  # type: ignore[index]
        business = {
            "candidate_id": candidate_id,
            "operation": "delete",
            "selection": {
                "surface": "draft",
                "anchor": {
                    "paragraph_id": paragraph["paragraph_id"],
                    "offset": 0,
                },
                "focus": {
                    "paragraph_id": paragraph["paragraph_id"],
                    "offset": 1,
                },
            },
            "accept_degraded": True,
        }
        body = _workspace_body(review, business)
        status, first = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, first
        status, readback = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, readback
        assert readback["draft_editor"]["candidate"] == first["draft_editor"]["candidate"]  # type: ignore[index]
        assert readback["draft_editor"]["workspace"] == first["draft_editor"]["workspace"]  # type: ignore[index]
        status, conflict = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={**body, "accept_degraded": False},
        )
        assert status == 409, conflict
        assert conflict["error"]["code"] == "draft_workspace_action_conflict"  # type: ignore[index]

        raw = json.dumps(business).encode()
        status, missing = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=raw,
        )
        assert status == 400, missing
        assert missing["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]


def test_approve_draft_requires_checkpoint_current_and_leaves_checkpoint_dormant(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        assert _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )[0] == 201
        initial = _request(review, "GET", "/api/workflow/draft-editor")[1]
        parent_id = initial["candidate"]["candidate_id"]  # type: ignore[index]
        paragraph = initial["paragraphs"][0]  # type: ignore[index]
        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": parent_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "accept_degraded": True,
            },
        )
        assert status == 201, changed
        current = changed["draft_editor"]
        child_id = current["candidate"]["candidate_id"]
        checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_review.json"
        before_checkpoint = checkpoint_path.read_bytes()
        wrong = {
            "content_draft_id": parent_id,
            "operation_id": (
                f"dwop_{current['workspace']['expected_checkpoint_ref']['generation']}"
                f"_{secrets.token_hex(16)}"
            ),
            **current["workspace"],
        }
        status, stale = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body=wrong,
        )
        assert status == 409, stale
        assert stale["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
        assert checkpoint_path.read_bytes() == before_checkpoint

        approve = {
            **wrong,
            "content_draft_id": child_id,
            "operation_id": (
                f"dwop_{current['workspace']['expected_checkpoint_ref']['generation']}"
                f"_{secrets.token_hex(16)}"
            ),
        }
        status, approved = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body=approve,
        )
        assert status == 200, approved
        assert checkpoint_path.read_bytes() == before_checkpoint
        assert workflow_status(root)["workflow_run"]["stage"] == "roughcut_review"
        assert _request(review, "GET", "/api/workflow/draft-editor")[0] == 400
        status, readback = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body=approve,
        )
        assert status == 200, readback
        assert readback["workflow_receipt"] == approved["workflow_receipt"]  # type: ignore[index]


def test_draft_agent_handoff_is_user_triggered_exact_and_zero_revision(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        before_project = (root / "project.json").read_bytes()
        before_revision = ProjectStore(root).load().revision

        status, snapshot = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, snapshot
        ordinary = json.dumps(snapshot, ensure_ascii=False)
        assert str(root) not in ordinary
        assert "review_session_" not in ordinary
        assert review.token not in ordinary

        status, copied = _request(
            review,
            "POST",
            "/api/workflow/draft-agent-handoff",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "quote": "你好😀",
            },
        )
        assert status == 200, copied
        handoff = copied["handoff"]  # type: ignore[index]
        assert handoff["candidate_id"] == candidate_id
        assert handoff["session_id"].startswith("review_session_")
        text = handoff["text"]
        assert str(root) not in text
        assert ProjectStore(root).load().project_id in text
        assert candidate_id in text
        assert handoff["session_id"] in text
        assert "你好😀" in text
        assert "当前选区引文：\n你好😀" in text
        assert "先读取上述精确 candidate" in text
        assert "不得扫描目录猜测最新稿" in text
        assert text.endswith("修改内容：")
        assert review.token not in text
        assert ProjectStore(root).load().revision == before_revision
        assert (root / "project.json").read_bytes() == before_project
        assert not (root / "proposals").exists()
        assert not (root / "edits").exists()

        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-agent-handoff",
            headers=_headers(review, write=True),
            body={"candidate_id": "draft_changed", "quote": None},
        )
        assert status == 400
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert (root / "project.json").read_bytes() == before_project


def test_stage_five_review_adoption_cannot_approve_or_start_export(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        _confirmed_id, proposal_id = _create_confirm_propose(review)
        before_renders = set((root / "renders").glob("*"))
        status, adopted = _request(
            review,
            "POST",
            "/api/roughcut-adopt",
            headers=_headers(review, write=True),
            body={
                "basis_id": proposal_id,
                "expected_revision": ProjectStore(root).load().revision,
            },
        )
        assert status == 200, adopted
        assert adopted["workflow_receipt"]["action"] == "adopt_roughcut"  # type: ignore[index]

    public_status = workflow_status(root, "wfr_review")
    assert public_status["workflow_run"]["stage"] == "export_review"
    assert public_status["workflow_run"]["lifecycle"] == "active"
    assert public_status["workflow_run"]["approval_refs"]["export"] is None
    assert public_status["allowed_actions"] == ["return_to_draft", "approve_export"]
    assert set((root / "renders").glob("*")) == before_renders


def test_roughcut_return_to_draft_is_zero_side_effect_and_first_edit_creates_child(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        confirmed_id, proposal_id = _create_confirm_propose(review)
        status, adopted = _request(
            review,
            "POST",
            "/api/roughcut-adopt",
            headers=_headers(review, write=True),
            body={
                "basis_id": proposal_id,
                "expected_revision": ProjectStore(root).load().revision,
            },
        )
        assert status == 200, adopted
        adopted_review = adopted["review"]  # type: ignore[index]
        decision_id = adopted_review["basis"]["id"]
        status, repeated_adoption = _request(
            review,
            "POST",
            "/api/roughcut-adopt",
            headers=_headers(review, write=True),
            body={
                "basis_id": proposal_id,
                "expected_revision": adopted["workflow_receipt"]["before"][  # type: ignore[index]
                    "project_revision"
                ],
            },
        )
        assert status == 200, repeated_adoption
        assert repeated_adoption["workflow_receipt"] == adopted["workflow_receipt"]  # type: ignore[index]
        assert repeated_adoption["adoption"]["changed"] is False  # type: ignore[index]
        before_project = (root / "project.json").read_bytes()
        before_revision = ProjectStore(root).load().revision
        proposal_path = root / "proposals" / f"{proposal_id}.json"
        decision_path = root / "edits" / f"{decision_id}.json"
        immutable_bytes = {
            proposal_path: proposal_path.read_bytes(),
            decision_path: decision_path.read_bytes(),
        }
        draft_paths_before = set((root / "content-drafts").glob("*.json"))

        status, rejected_origin = _request(
            review,
            "POST",
            "/api/roughcut-return-draft",
            headers={
                **_headers(review, write=True),
                "Origin": "https://evil.test",
            },
            body={
                "basis_id": decision_id,
                "expected_revision": before_revision,
            },
        )
        assert status == 403, rejected_origin
        status, returned = _request(
            review,
            "POST",
            "/api/roughcut-return-draft",
            headers=_headers(review, write=True),
            body={
                "basis_id": decision_id,
                "expected_revision": before_revision,
            },
        )
        assert status == 200, returned
        editor = returned["draft_editor"]  # type: ignore[index]
        assert editor["candidate"] == {
            "candidate_id": confirmed_id,
            "parent_candidate_id": editor["candidate"]["parent_candidate_id"],
            "display_title": None,
            "confirmed_by_user": True,
            "has_unrecorded_narration": False,
        }
        assert (root / "project.json").read_bytes() == before_project
        assert set((root / "content-drafts").glob("*.json")) == draft_paths_before
        assert all(path.read_bytes() == data for path, data in immutable_bytes.items())
        status, repeated_return = _request(
            review,
            "POST",
            "/api/roughcut-return-draft",
            headers=_headers(review, write=True),
            body={
                "basis_id": decision_id,
                "expected_revision": before_revision,
            },
        )
        assert status == 200, repeated_return
        assert repeated_return["workflow_receipt"] == returned["workflow_receipt"]  # type: ignore[index]

        paragraph = editor["paragraphs"][0]
        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": confirmed_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "accept_degraded": True,
            },
        )
        assert status == 201, changed
        child_id = changed["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert child_id != confirmed_id
        assert changed["draft_editor"]["candidate"]["parent_candidate_id"] == confirmed_id  # type: ignore[index]
        assert changed["draft_editor"]["candidate"]["confirmed_by_user"] is False  # type: ignore[index]
        assert ProjectStore(root).load().revision == before_revision
        assert (root / "project.json").read_bytes() == before_project
        assert all(path.read_bytes() == data for path, data in immutable_bytes.items())

        status, undone = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": child_id},
        )
        assert status == 200, undone
        assert undone["draft_editor"]["candidate"]["candidate_id"] == confirmed_id  # type: ignore[index]
        status, redone = _request(
            review,
            "POST",
            "/api/workflow/draft-redo",
            headers=_headers(review, write=True),
            body={"candidate_id": confirmed_id},
        )
        assert status == 200, redone
        assert redone["draft_editor"]["candidate"]["candidate_id"] == child_id  # type: ignore[index]
        assert (root / "project.json").read_bytes() == before_project
        assert all(path.read_bytes() == data for path, data in immutable_bytes.items())

        status, confirmed = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body={"content_draft_id": child_id},
        )
        assert status == 200, confirmed
        new_confirmed_id = confirmed["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        status, new_preview = _request(
            review,
            "POST",
            "/api/workflow/content-draft-propose",
            headers=_headers(review, write=True),
            body={"content_draft_id": new_confirmed_id},
        )
        assert status == 200, new_preview
        assert new_preview["review"]["basis"]["type"] == "proposal"  # type: ignore[index]
        assert new_preview["review"]["basis"]["id"] != proposal_id  # type: ignore[index]
        assert ProjectStore(root).load().active_edit_version_id == decision_id
        assert all(path.read_bytes() == data for path, data in immutable_bytes.items())


def test_review_submit_reopens_exact_rebase_after_confirmed_return_checkpoint(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        confirmed_id, proposal_id = _create_confirm_propose(review)
        status, adopted = _request(
            review,
            "POST",
            "/api/roughcut-adopt",
            headers=_headers(review, write=True),
            body={
                "basis_id": proposal_id,
                "expected_revision": ProjectStore(root).load().revision,
            },
        )
        assert status == 200, adopted
        decision_id = adopted["review"]["basis"]["id"]  # type: ignore[index]
        status, returned = _request(
            review,
            "POST",
            "/api/roughcut-return-draft",
            headers=_headers(review, write=True),
            body={
                "basis_id": decision_id,
                "expected_revision": ProjectStore(root).load().revision,
            },
        )
        assert status == 200, returned
        returned_editor = returned["draft_editor"]  # type: ignore[index]
        returned_generation = returned_editor["workspace"][  # type: ignore[index]
            "expected_checkpoint_ref"
        ]["generation"]
        assert returned_editor["candidate"]["candidate_id"] == confirmed_id  # type: ignore[index]
        assert returned_editor["candidate"]["confirmed_by_user"] is True  # type: ignore[index]

        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={
                "parent_draft_id": confirmed_id,
                "blocks": _source_blocks(),
            },
        )
        assert status == 201, created
        new_candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        assert new_candidate_id != confirmed_id
        status, reopened = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, reopened
        assert reopened["candidate"]["candidate_id"] == new_candidate_id  # type: ignore[index]
        assert reopened["candidate"]["confirmed_by_user"] is False  # type: ignore[index]
        assert reopened["history"]["can_redo"] is False  # type: ignore[index]
        assert reopened["workspace"]["expected_checkpoint_ref"]["generation"] == (  # type: ignore[index]
            returned_generation + 1
        )

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as restarted:
        status, reopened = _request(
            restarted,
            "GET",
            "/api/workflow/draft-editor",
        )
        assert status == 200, reopened
        assert reopened["candidate"]["candidate_id"] == new_candidate_id  # type: ignore[index]
        assert reopened["candidate"]["confirmed_by_user"] is False  # type: ignore[index]
        assert reopened["history"]["can_redo"] is False  # type: ignore[index]
        assert reopened["workspace"]["expected_checkpoint_ref"]["generation"] == (  # type: ignore[index]
            returned_generation + 1
        )


def test_review_submit_derives_exact_current_parent_and_rejects_stale_siblings(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, first = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, first
        first_id = first["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]

        project = ProjectStore(root).load()
        assert project.active_brief_id is not None
        context = read_multi_source_agent_context(
            root,
            source_bindings=fixture["bindings"],  # type: ignore[arg-type]
            brief_id=project.active_brief_id,
            expected_revision=project.revision,
            offset=0,
            limit=20,
        )
        sibling = create_content_draft(
            root,
            parent_draft_id=first_id,
            source_bindings=fixture["bindings"],  # type: ignore[arg-type]
            brief_id=project.active_brief_id,
            context_hash=context.context_hash,
            blocks=_source_blocks(),
            expected_revision=project.revision,
        ).content_draft

        status, second = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, second
        second_draft = second["content_draft_mutation"]["content_draft"]  # type: ignore[index]
        second_id = second_draft["content_draft_id"]
        assert second_draft["parent_draft_id"] == first_id

        status, explicit = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"parent_draft_id": second_id, "blocks": _source_blocks()},
        )
        assert status == 201, explicit
        explicit_draft = explicit["content_draft_mutation"]["content_draft"]  # type: ignore[index]
        assert explicit_draft["parent_draft_id"] == second_id

        before = sorted((root / "content-drafts").glob("*.json"))
        for rejected_parent in (first_id, sibling.content_draft_id):
            status, rejected = _request(
                review,
                "POST",
                "/api/workflow/content-drafts",
                headers=_headers(review, write=True),
                body={
                    "parent_draft_id": rejected_parent,
                    "blocks": _source_blocks(),
                },
            )
            assert status == 400, rejected
            assert rejected["error"]["code"] == "workflow_subject_mismatch"  # type: ignore[index]
        assert sorted((root / "content-drafts").glob("*.json")) == before

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as restarted:
        status, refreshed = _request(
            restarted,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(restarted, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, refreshed
        refreshed_draft = refreshed["content_draft_mutation"]["content_draft"]  # type: ignore[index]
        assert refreshed_draft["parent_draft_id"] == explicit_draft["content_draft_id"]


def test_compound_cross_source_delete_move_undo_redo_preserve_artifacts(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path, compound=True)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    _context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    parent_id = _seed_workflow_draft(
        root,
        bindings=fixture["bindings"],  # type: ignore[arg-type]
        blocks=_compound_source_blocks(),
    )
    immutable_paths = (
        root / "project.json",
        root / "transcripts" / "src_a" / "tr_a.json",
        root / "transcripts" / "src_b" / "tr_b.json",
        root / "content-drafts" / f"{parent_id}.json",
    )
    immutable_bytes = {path: path.read_bytes() for path in immutable_paths}

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        content_draft_id=parent_id,
        static_root=_static(tmp_path),
    ) as review:
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        compound = next(
            paragraph
            for paragraph in editor["paragraphs"]  # type: ignore[index]
            if len(paragraph["exact_refs"]) == 12
        )
        tail = next(
            paragraph
            for paragraph in editor["paragraphs"]  # type: ignore[index]
            if "收尾段" in paragraph["text"]
        )
        selection_request = {
            "surface": "draft",
            "anchor": {"paragraph_id": compound["paragraph_id"], "offset": 0},
            "focus": {
                "paragraph_id": compound["paragraph_id"],
                "offset": len(compound["text"]),
            },
        }
        status, selected = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={"candidate_id": parent_id, **selection_request},
        )
        assert status == 200, selected
        expected_ids = [
            *(f"seg_b_{index}" for index in range(6)),
            *(f"seg_a_{index}" for index in range(6)),
        ]
        assert [
            ref["segment_id"] for ref in selected["resolution"]["refs"]  # type: ignore[index]
        ] == expected_ids
        assert [
            group["source_id"]
            for group in selected["correspondence_groups"]  # type: ignore[index]
        ] == ["src_b", "src_a"]
        assert len(selected["correspondence_groups"]) == 2  # type: ignore[arg-type]

        status, reverse_selected = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": parent_id,
                "surface": "draft",
                "anchor": selection_request["focus"],
                "focus": selection_request["anchor"],
            },
        )
        assert status == 200, reverse_selected
        assert reverse_selected["resolution"]["direction"] == "backward"  # type: ignore[index]
        assert reverse_selected["correspondence_groups"] == selected["correspondence_groups"]  # type: ignore[index]

        status, deleted = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": parent_id,
                "operation": "delete",
                "selection": selection_request,
                "accept_degraded": False,
            },
        )
        assert status == 201, deleted
        deleted_id = deleted["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        deleted_path = root / "content-drafts" / f"{deleted_id}.json"
        deleted_bytes = deleted_path.read_bytes()
        assert [
            ref["segment_id"]
            for paragraph in deleted["draft_editor"]["paragraphs"]  # type: ignore[index]
            for ref in paragraph["exact_refs"]
        ] == ["seg_a_tail"]

        status, undone = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": deleted_id},
        )
        assert status == 200, undone
        assert undone["draft_editor"]["candidate"]["candidate_id"] == parent_id  # type: ignore[index]
        status, redone = _request(
            review,
            "POST",
            "/api/workflow/draft-redo",
            headers=_headers(review, write=True),
            body={"candidate_id": parent_id},
        )
        assert status == 200, redone
        assert redone["draft_editor"]["candidate"]["candidate_id"] == deleted_id  # type: ignore[index]
        status, restored = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": deleted_id},
        )
        assert status == 200, restored

        reverse_selection = {
            "surface": "draft",
            "anchor": {
                "paragraph_id": compound["paragraph_id"],
                "offset": len(compound["text"]),
            },
            "focus": {"paragraph_id": compound["paragraph_id"], "offset": 0},
        }
        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": parent_id,
                "operation": "move",
                "selection": reverse_selection,
                "caret": {
                    "paragraph_id": tail["paragraph_id"],
                    "offset": len(tail["text"]),
                },
                "accept_degraded": False,
            },
        )
        assert status == 201, moved
        moved_id = moved["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        moved_path = root / "content-drafts" / f"{moved_id}.json"
        moved_bytes = moved_path.read_bytes()
        assert [
            ref["segment_id"]
            for paragraph in moved["draft_editor"]["paragraphs"]  # type: ignore[index]
            for ref in paragraph["exact_refs"]
        ] == ["seg_a_tail", *expected_ids]
        status, moved_undo = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": moved_id},
        )
        assert status == 200, moved_undo
        status, moved_redo = _request(
            review,
            "POST",
            "/api/workflow/draft-redo",
            headers=_headers(review, write=True),
            body={"candidate_id": parent_id},
        )
        assert status == 200, moved_redo
        assert moved_redo["draft_editor"]["candidate"]["candidate_id"] == moved_id  # type: ignore[index]

    for path, before in immutable_bytes.items():
        assert path.read_bytes() == before
    assert deleted_path.read_bytes() == deleted_bytes
    assert moved_path.read_bytes() == moved_bytes


def test_draft_editor_session_prebuilds_once_and_reuses_cache_for_hot_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    _context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    candidate_id = _seed_workflow_draft(
        root,
        bindings=fixture["bindings"],  # type: ignore[arg-type]
        blocks=_source_blocks(),
    )
    calls = 0
    transcript_base_calls = 0
    original = review_server_module.load_draft_editor_snapshot_from_workflow
    original_transcript_base = draft_editor_module.load_draft_editor_transcript_base

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    def counted_transcript_base(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal transcript_base_calls
        transcript_base_calls += 1
        return original_transcript_base(*args, **kwargs)

    monkeypatch.setattr(
        review_server_module,
        "load_draft_editor_snapshot_from_workflow",
        counted,
    )
    monkeypatch.setattr(
        draft_editor_module,
        "load_draft_editor_transcript_base",
        counted_transcript_base,
    )
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        content_draft_id=candidate_id,
        static_root=_static(tmp_path),
    ) as review:
        assert calls == 1
        assert transcript_base_calls == 1
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        assert calls == 1
        paragraph = editor["paragraphs"][0]  # type: ignore[index]

        status, caret = _request(
            review,
            "POST",
            "/api/workflow/draft-caret-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "paragraph_id": paragraph["paragraph_id"],
                "offset": 0,
            },
        )
        assert status == 200, caret
        status, searched = _request(
            review,
            "POST",
            "/api/workflow/draft-search",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "surface": "source",
                "query": "重复",
                "offset": 0,
                "limit": 20,
            },
        )
        assert status == 200, searched
        source_paragraph_id = searched["matches"][0]["paragraph_id"]  # type: ignore[index]
        status, window = _request(
            review,
            "POST",
            "/api/workflow/draft-transcript-window",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "source_id": "src_a",
                "offset": 0,
                "limit": 2,
                "paragraph_id": source_paragraph_id,
            },
        )
        assert status == 200, window
        assert any(
            item["paragraph_id"] == source_paragraph_id
            for item in window["paragraphs"]  # type: ignore[index]
        )
        assert calls == 1

        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "accept_degraded": True,
            },
        )
        assert status == 201, changed
        child_id = changed["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert child_id != candidate_id
        assert calls == 2
        assert transcript_base_calls == 1
        assert _request(review, "GET", "/api/workflow/draft-editor")[0] == 200
        assert calls == 2
        assert transcript_base_calls == 1


def test_500_plus_draft_edit_reuses_validated_workflow_and_transcript_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _workflow_project(tmp_path, extra_segments=520)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    _context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    candidate_id = _seed_workflow_draft(
        root,
        bindings=fixture["bindings"],  # type: ignore[arg-type]
        blocks=_source_blocks(),
    )

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        content_draft_id=candidate_id,
        static_root=_static(tmp_path),
    ) as review:
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        status, window = _request(
            review,
            "POST",
            "/api/workflow/draft-transcript-window",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "source_id": "src_a",
                "offset": 0,
                "limit": 20,
            },
        )
        assert status == 200, window
        assert window["total"] >= 500  # type: ignore[index]

        calls = {
            "workflow": 0,
            "transcript_base": 0,
            "draft_transcript": 0,
            "content_draft_transcript": 0,
        }
        original_workflow = review_server_module.load_workflow_review_snapshot
        original_base = draft_editor_module.load_draft_editor_transcript_base
        original_draft_transcript = draft_editor_module._read_transcript
        original_content_transcript = content_drafts_module._read_transcript

        def counted_workflow(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["workflow"] += 1
            return original_workflow(*args, **kwargs)

        def counted_base(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["transcript_base"] += 1
            return original_base(*args, **kwargs)

        def counted_draft_transcript(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["draft_transcript"] += 1
            return original_draft_transcript(*args, **kwargs)

        def counted_content_transcript(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["content_draft_transcript"] += 1
            return original_content_transcript(*args, **kwargs)

        monkeypatch.setattr(
            review_server_module,
            "load_workflow_review_snapshot",
            counted_workflow,
        )
        monkeypatch.setattr(
            draft_editor_module,
            "load_draft_editor_transcript_base",
            counted_base,
        )
        monkeypatch.setattr(
            draft_editor_module,
            "_read_transcript",
            counted_draft_transcript,
        )
        monkeypatch.setattr(
            content_drafts_module,
            "_read_transcript",
            counted_content_transcript,
        )

        paragraph = editor["paragraphs"][0]  # type: ignore[index]
        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "accept_degraded": True,
            },
        )
        assert status == 201, changed
        assert changed["draft_editor"]["candidate"]["candidate_id"] != candidate_id  # type: ignore[index]
        assert calls == {
            "workflow": 0,
            "transcript_base": 0,
            "draft_transcript": 0,
            "content_draft_transcript": 0,
        }
        _assert_edit_timing(changed, "delete")  # type: ignore[arg-type]


def test_review_session_handoff_selects_one_exact_scoped_child_and_refreshes(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    _context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    blocks = _schema2_section_blocks()
    parent_id = _seed_workflow_draft(
        root,
        bindings=fixture["bindings"],  # type: ignore[arg-type]
        blocks=blocks,
    )
    revised_blocks = [dict(block) for block in blocks]
    revised_blocks[0] = {**revised_blocks[0], "title": "新开头"}
    child = revise_content_draft_scoped(
        root,
        parent_draft_id=parent_id,
        mutable_block_ids=["heading_one"],
        blocks=revised_blocks,
        expected_revision=project.revision,
    )
    child_id = child.content_draft.content_draft_id
    immutable_paths = (
        root / "project.json",
        root / "content-drafts" / f"{parent_id}.json",
        root / "content-drafts" / f"{child_id}.json",
    )
    immutable_bytes = {path: path.read_bytes() for path in immutable_paths}

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        content_draft_id=parent_id,
        static_root=_static(tmp_path),
    ) as review:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert initial["candidate"]["candidate_id"] == parent_id  # type: ignore[index]
        assert initial["candidate_handoff"] == {  # type: ignore[index]
            "endpoint": "/api/workflow/draft-candidate-select",
            "requires_exact_parent": True,
        }

        status, forbidden = _request(
            review,
            "POST",
            "/api/workflow/draft-candidate-select",
            body={
                "parent_candidate_id": parent_id,
                "child_candidate_id": child_id,
            },
            headers={
                "Origin": f"http://127.0.0.1:{review.port}",
                "Content-Type": "application/json",
            },
        )
        assert status == 403
        assert forbidden["error"]["code"] == "forbidden"  # type: ignore[index]

        status, wrong_parent = _request(
            review,
            "POST",
            "/api/workflow/draft-candidate-select",
            body={
                "parent_candidate_id": child_id,
                "child_candidate_id": child_id,
            },
            headers=_headers(review, write=True),
        )
        assert status == 400
        assert wrong_parent["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert _request(
            review,
            "GET",
            "/api/workflow/draft-editor",
        )[1]["candidate"]["candidate_id"] == parent_id  # type: ignore[index]

        status, selected = _request(
            review,
            "POST",
            "/api/workflow/draft-candidate-select",
            body={
                "parent_candidate_id": parent_id,
                "child_candidate_id": child_id,
            },
            headers=_headers(review, write=True),
        )
        assert status == 200, selected
        assert selected["draft_editor"]["candidate"]["candidate_id"] == child_id  # type: ignore[index]
        assert selected["draft_editor"]["candidate"]["parent_candidate_id"] == parent_id  # type: ignore[index]
        assert selected["draft_editor"]["candidate"]["confirmed_by_user"] is False  # type: ignore[index]
        assert selected["draft_editor"]["history"] == {  # type: ignore[index]
            "can_undo": True,
            "can_redo": False,
            "redo_scope": "draft_workspace",
        }
        status, refreshed = _request(
            review,
            "GET",
            "/api/workflow/draft-editor",
        )
        assert status == 200, refreshed
        assert refreshed["candidate"]["candidate_id"] == child_id  # type: ignore[index]
        assert any(
            paragraph["section_title"] == "新开头"
            for paragraph in refreshed["paragraphs"]  # type: ignore[index]
        )

    for path, before in immutable_bytes.items():
        assert path.read_bytes() == before
    assert ProjectStore(root).load().revision == project.revision
    assert ProjectStore(root).load().active_content_draft_id is None


def test_review_session_handoff_rejects_non_child_scope_and_rebuild_failure_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    _context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    blocks = _schema2_section_blocks()
    parent_id = _seed_workflow_draft(
        root,
        bindings=fixture["bindings"],  # type: ignore[arg-type]
        blocks=blocks,
    )
    child_blocks = [dict(block) for block in blocks]
    child_blocks[0] = {**child_blocks[0], "title": "第一轮"}
    child = revise_content_draft_scoped(
        root,
        parent_draft_id=parent_id,
        mutable_block_ids=["heading_one"],
        blocks=child_blocks,
        expected_revision=project.revision,
    )
    child_id = child.content_draft.content_draft_id
    grandchild_blocks = [
        {
            key: value
            for key, value in block.to_dict().items()
            if not (block.kind == "source_excerpt" and key == "section_title")
        }
        for block in child.content_draft.blocks
    ]
    grandchild_blocks[0] = {**grandchild_blocks[0], "title": "第二轮"}
    grandchild = revise_content_draft_scoped(
        root,
        parent_draft_id=child_id,
        mutable_block_ids=["heading_one"],
        blocks=grandchild_blocks,
        expected_revision=project.revision,
    )
    grandchild_id = grandchild.content_draft.content_draft_id
    narrow_child = create_content_draft(
        root,
        parent_draft_id=parent_id,
        source_bindings=[fixture["bindings"][1]],  # type: ignore[index]
        brief_id=project.active_brief_id,
        context_hash=read_agent_context(
            root,
            source_id="src_a",
            transcript_version_id="tr_a",
            brief_id=project.active_brief_id,
            expected_revision=project.revision,
            offset=0,
            limit=20,
        ).context_hash,
        blocks=[blocks[3]],
        expected_revision=project.revision,
    )

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        content_draft_id=parent_id,
        static_root=_static(tmp_path),
    ) as review:
        for rejected_id in (
            grandchild_id,
            narrow_child.content_draft.content_draft_id,
        ):
            status, rejected = _request(
                review,
                "POST",
                "/api/workflow/draft-candidate-select",
                body={
                    "parent_candidate_id": parent_id,
                    "child_candidate_id": rejected_id,
                },
                headers=_headers(review, write=True),
            )
            if rejected_id == grandchild_id:
                assert status == 400, rejected
                assert rejected["error"]["code"] == (
                    "draft_workspace_integrity_error"
                )  # type: ignore[index]
            else:
                assert status == 409, rejected
                assert rejected["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
            current = _request(
                review,
                "GET",
                "/api/workflow/draft-editor",
            )[1]
            assert current["candidate"]["candidate_id"] == parent_id  # type: ignore[index]

        original = review_server_module.load_draft_editor_snapshot_from_workflow
        fail_once = True

        def injected(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise OSError("injected handoff rebuild failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(
            review_server_module,
            "load_draft_editor_snapshot_from_workflow",
            injected,
        )
        select_body = _workspace_body(
            review,
            {
                "parent_candidate_id": parent_id,
                "child_candidate_id": child_id,
            },
        )
        status, failed = _request(
            review,
            "POST",
            "/api/workflow/draft-candidate-select",
            body=select_body,
            headers=_headers(review, write=True),
        )
        assert status == 500
        assert failed["error"]["code"] == "review_service_failed"  # type: ignore[index]
        status, retained = _request(
            review,
            "GET",
            "/api/workflow/draft-editor",
        )
        assert status == 409, retained
        assert retained["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
        status, readback = _request(
            review,
            "POST",
            "/api/workflow/draft-candidate-select",
            body=select_body,
            headers=_headers(review, write=True),
        )
        assert status == 200, readback
        assert readback["draft_editor"]["candidate"]["candidate_id"] == child_id  # type: ignore[index]


def test_draft_editor_rebuild_failure_keeps_previous_cached_snapshot_and_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    _context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    candidate_id = _seed_workflow_draft(
        root,
        bindings=fixture["bindings"],  # type: ignore[arg-type]
        blocks=_source_blocks(),
    )
    parent_path = root / "content-drafts" / f"{candidate_id}.json"
    parent_bytes = parent_path.read_bytes()
    before_artifacts = {
        path.name for path in (root / "content-drafts").glob("*.json")
    }
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        content_draft_id=candidate_id,
        static_root=_static(tmp_path),
    ) as review:
        editor = _request(review, "GET", "/api/workflow/draft-editor")[1]
        paragraph = editor["paragraphs"][0]  # type: ignore[index]
        original = review_server_module.load_draft_editor_snapshot_from_workflow
        fail_once = True

        def injected(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise OSError("injected snapshot rebuild failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(
            review_server_module,
            "load_draft_editor_snapshot_from_workflow",
            injected,
        )
        edit_body = _workspace_body(
            review,
            {
                "candidate_id": candidate_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "accept_degraded": True,
            },
        )
        status, failed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=edit_body,
        )
        assert status == 500
        assert failed["error"]["code"] == "review_service_failed"  # type: ignore[index]
        status, still_current = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 409, still_current
        assert still_current["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
        status, recovered = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=edit_body,
        )
        assert status == 201, recovered
        child_id = recovered["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert child_id != candidate_id
        assert parent_path.read_bytes() == parent_bytes
        assert {
            path.name for path in (root / "content-drafts").glob("*.json")
        } == before_artifacts | {f"{child_id}.json"}


def test_draft_editor_rejects_an_external_revision_without_creating_a_child(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        before_artifacts = {
            path.name for path in (root / "content-drafts").glob("*.json")
        }
        current = ProjectStore(root).load()
        ProjectStore(root).save(
            replace(current, revision=current.revision + 1),
            expected_revision=current.revision,
        )

        status, stale = _request(
            review,
            "POST",
            "/api/workflow/draft-search",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "surface": "draft",
                "query": "你好",
                "offset": 0,
                "limit": 20,
            },
        )

        assert status == 409
        assert stale["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
        assert {
            path.name for path in (root / "content-drafts").glob("*.json")
        } == before_artifacts


def test_draft_editor_cache_rejects_active_transcript_change_at_same_revision(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        current = ProjectStore(root).load()
        ProjectStore(root).save(
            replace(
                current,
                active_transcript_versions={
                    **current.active_transcript_versions,
                    "src_a": "tr_external",
                },
            ),
            expected_revision=current.revision,
        )

        status, stale = _request(
            review,
            "POST",
            "/api/workflow/draft-search",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "surface": "source",
                "query": "重复",
                "offset": 0,
                "limit": 20,
            },
        )

        assert status == 409
        assert stale["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]


def test_confirming_after_undo_clears_session_redo_history(tmp_path: Path) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        editor = _request(review, "GET", "/api/workflow/draft-editor")[1]
        paragraph = next(
            item
            for item in editor["paragraphs"]  # type: ignore[index]
            if "你好😀世界。" in item["text"]
        )
        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "operation": "delete",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "accept_degraded": False,
            },
        )
        assert status == 201, changed
        child_id = changed["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]

        status, undone = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": child_id},
        )
        assert status == 200, undone
        assert undone["draft_editor"]["history"]["can_redo"] is True  # type: ignore[index]

        status, confirmed = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body={"content_draft_id": candidate_id},
        )
        assert status == 200, confirmed
        confirmed_id = confirmed["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        status, preview = _request(
            review,
            "POST",
            "/api/workflow/content-draft-propose",
            headers=_headers(review, write=True),
            body={"content_draft_id": confirmed_id},
        )
        assert status == 200, preview
        proposal_id = preview["review"]["basis"]["id"]  # type: ignore[index]
        status, returned = _request(
            review,
            "POST",
            "/api/roughcut-return-draft",
            headers=_headers(review, write=True),
            body={
                "basis_id": proposal_id,
                "expected_revision": ProjectStore(root).load().revision,
            },
        )
        assert status == 200, returned
        assert returned["draft_editor"]["history"]["can_redo"] is False  # type: ignore[index]


def test_draft_editor_api_insert_requires_degraded_acceptance_and_move_is_exact(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    initial_revision = ProjectStore(root).load().revision
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        draft_paragraph = next(
            item
            for item in editor["paragraphs"]  # type: ignore[index]
            if "你好😀世界。" in item["text"]
        )
        status, transcript = _request(
            review,
            "POST",
            "/api/workflow/readable-transcript",
            headers=_headers(review, write=True),
            body={"offset": 0, "limit": 20},
        )
        assert status == 200, transcript
        source_paragraph = next(
            item
            for item in transcript["paragraphs"]  # type: ignore[index]
            if item["source_id"] == "src_a" and "重复。" in item["text"]
        )
        repeat_offset = source_paragraph["text"].index("重复。")
        insert_body = {
            "candidate_id": candidate_id,
            "operation": "insert",
            "selection": {
                "surface": "source",
                "anchor": {
                    "paragraph_id": source_paragraph["paragraph_id"],
                    "offset": repeat_offset,
                },
                "focus": {
                    "paragraph_id": source_paragraph["paragraph_id"],
                    "offset": repeat_offset + 1,
                },
            },
            "caret": {
                "paragraph_id": draft_paragraph["paragraph_id"],
                "offset": len(draft_paragraph["text"]),
            },
            "accept_degraded": False,
        }
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=insert_body,
        )
        assert status == 400
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]

        insert_body["accept_degraded"] = True
        status, inserted = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=insert_body,
        )
        assert status == 201, inserted
        _assert_edit_timing(inserted, "insert")  # type: ignore[arg-type]
        inserted_id = inserted["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert inserted["selection"]["resolution"]["degraded"] is True  # type: ignore[index]
        inserted_paragraph = next(
            item
            for item in inserted["draft_editor"]["paragraphs"]  # type: ignore[index]
            if "你好😀世界。" in item["text"]
        )
        assert inserted_paragraph["text"].endswith("重复。")

        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={
                "candidate_id": inserted_id,
                "operation": "move",
                "selection": {
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": inserted_paragraph["paragraph_id"],
                        "offset": 0,
                    },
                    "focus": {
                        "paragraph_id": inserted_paragraph["paragraph_id"],
                        "offset": 1,
                    },
                },
                "caret": {
                    "paragraph_id": inserted_paragraph["paragraph_id"],
                    "offset": len(inserted_paragraph["text"]),
                },
                "accept_degraded": False,
            },
        )
        assert status == 201, moved
        _assert_edit_timing(moved, "move")  # type: ignore[arg-type]
        moved_paragraph = next(
            item
            for item in moved["draft_editor"]["paragraphs"]  # type: ignore[index]
            if "好😀世界。" in item["text"]
        )
        assert moved_paragraph["text"].endswith("重复。你")
        assert ProjectStore(root).load().revision == initial_revision


def test_draft_editor_narration_update_stays_unrecorded_and_does_not_propose(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        blocks = _source_blocks() + [
            {
                "block_id": "block_narration",
                "kind": "narration",
                "text": "旧解说",
                "status": "draft",
                "recorded_refs": [],
            }
        ]
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": blocks},
        )
        assert status == 201
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        status, changed = _request(
            review,
            "POST",
            "/api/workflow/draft-narration",
            headers=_headers(review, write=True),
            body={
                "candidate_id": candidate_id,
                "block_id": "block_narration",
                "text": "更新后的待录音解说",
            },
        )
        assert status == 201, changed
        assert changed["draft_editor"]["candidate"]["has_unrecorded_narration"] is True  # type: ignore[index]
        narration = next(
            item
            for item in changed["draft_editor"]["paragraphs"]  # type: ignore[index]
            if item["kind"] == "narration"
        )
        assert narration["text"] == "更新后的待录音解说"
        assert narration["narration_status"] == "draft"
        assert not (root / "proposals").exists()
        assert not (root / "edits").exists()


def test_schema2_section_titles_confirm_once_builds_roughcut_preview_and_readback(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    initial_revision = ProjectStore(root).load().revision
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _schema2_section_blocks()},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"]["content_draft_id"]  # type: ignore[index]
        confirm_body = _workspace_body(review, {"content_draft_id": candidate_id})
        status, confirmed = _request(
            review, "POST", "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body=confirm_body,
        )
        assert status == 200, confirmed
        confirmed_draft = confirmed["content_draft_mutation"]["content_draft"]  # type: ignore[index]
        assert confirmed_draft["parent_draft_id"] == candidate_id  # type: ignore[index]
        assert confirmed_draft["confirmed_by_user"] is True  # type: ignore[index]
        assert workflow_status(root)["workflow_run"]["stage"] == "roughcut_review"  # type: ignore[index]
        counts = (
            len(list((root / "content-drafts").glob("*.json"))),
            len(list((root / "proposals").glob("proposal_*.json"))),
            ProjectStore(root).load().revision,
        )
        assert counts == (2, 1, initial_revision + 1)

        status, repeated = _request(
            review, "POST", "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body=confirm_body,
        )
        assert status == 200, repeated
        assert repeated["workflow_receipt"] == confirmed["workflow_receipt"]  # type: ignore[index]
        assert repeated["content_draft_mutation"] == confirmed["content_draft_mutation"]  # type: ignore[index]
        assert (
            len(list((root / "content-drafts").glob("*.json"))),
            len(list((root / "proposals").glob("proposal_*.json"))),
            ProjectStore(root).load().revision,
        ) == counts


def test_content_draft_confirm_refreshes_once_and_propose_switches_review_mode(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    initial_revision = ProjectStore(root).load().revision
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        _confirmed_id, proposal_id = _create_confirm_propose(review)
        assert ProjectStore(root).load().revision == initial_revision + 1
        assert ProjectStore(root).load().active_edit_version_id is None
        assert not (root / "edits").exists()
        assert _request(review, "GET", "/api/workflow")[0] == 409
        status, snapshot = _request(review, "GET", "/api/review")
        assert status == 200
        assert snapshot["basis"] == {"type": "proposal", "id": proposal_id}  # type: ignore[index]
        status, diff = _request(review, "GET", "/api/proposal-diff")
        assert status == 200
        assert diff == {
            "applicable": False,
            "reason": "no_active_decision",
            "project_revision": initial_revision + 1,
        }


def test_content_draft_proposal_retry_is_idempotent_after_review_handoff(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    initial_revision = ProjectStore(root).load().revision
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        confirmed_id, proposal_id = _create_confirm_propose(review)
        before_proposals = {
            path.name for path in (root / "proposals").glob("proposal_*.json")
        }
        status, retried = _request(
            review,
            "POST",
            "/api/workflow/content-draft-propose",
            headers=_headers(review, write=True),
            body={"content_draft_id": confirmed_id},
        )
        assert status == 200, retried
        assert retried["content_draft_proposal"]["content_draft_id"] == confirmed_id  # type: ignore[index]
        assert retried["content_draft_proposal"]["proposal"]["proposal_id"] == proposal_id  # type: ignore[index]
        assert retried["review"]["basis"] == {"type": "proposal", "id": proposal_id}  # type: ignore[index]
        assert {
            path.name for path in (root / "proposals").glob("proposal_*.json")
        } == before_proposals
        assert ProjectStore(root).load().revision == initial_revision + 1
        assert ProjectStore(root).load().active_edit_version_id is None
        assert not (root / "edits").exists()
        assert not (root / "renders").exists()


def test_approve_draft_failure_keeps_review_candidate_and_retry_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    initial_revision = ProjectStore(root).load().revision
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        original = review_server_module.workflow_action
        fail_once = True

        def injected(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal fail_once
            if fail_once and args[3] == "approve_draft":
                fail_once = False
                raise OSError("injected approve_draft failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(review_server_module, "workflow_action", injected)
        status, failed = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body={"content_draft_id": candidate_id},
        )
        assert status == 500, failed
        status, retained = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, retained
        assert retained["candidate"]["candidate_id"] == candidate_id  # type: ignore[index]
        assert retained["candidate"]["confirmed_by_user"] is False  # type: ignore[index]

        status, retried = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body={"content_draft_id": candidate_id},
        )
        assert status == 200, retried
        assert ProjectStore(root).load().revision == initial_revision + 1
        assert len(list((root / "proposals").glob("proposal_*.json"))) == 1
        assert ProjectStore(root).load().active_edit_version_id is None
        assert not (root / "edits").exists()
        assert not (root / "renders").exists()


def test_single_source_confirm_and_propose_enters_schema_one_roughcut_review(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path, workflow_source_ids=("src_a",))
    root = Path(fixture["root"])
    binding = fixture["bindings"]
    with start_review_server(
        root,
        source_bindings=binding,
        static_root=_static(tmp_path),
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": [_source_blocks()[1]]},
        )
        assert status == 201, created
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        status, confirmed = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body={"content_draft_id": candidate_id},
        )
        assert status == 200, confirmed
        confirmed_id = confirmed["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        status, proposed = _request(
            review,
            "POST",
            "/api/workflow/content-draft-propose",
            headers=_headers(review, write=True),
            body={"content_draft_id": confirmed_id},
        )
        assert status == 200, proposed
        assert proposed["content_draft_proposal"]["proposal_schema_version"] == 1  # type: ignore[index]
        assert proposed["review"]["schema_version"] == 1  # type: ignore[index]
        assert proposed["review"]["basis"]["type"] == "proposal"  # type: ignore[index]
        assert ProjectStore(root).load().active_edit_version_id is None
        assert not (root / "edits").exists()
        assert not (root / "renders").exists()


def test_proposal_coverage_pages_use_core_overlay_after_workflow_handoff(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        confirmed_id, proposal_id = _create_confirm_propose(review)
        paragraphs: list[dict[str, object]] = []
        offset = 0
        view_hash: str | None = None
        while True:
            status, page = _request(
                review,
                "POST",
                "/api/proposal-coverage",
                headers=_headers(review, write=True),
                body={"offset": offset, "limit": 1},
            )
            assert status == 200, page
            assert page["artifact"] == {  # type: ignore[index]
                "basis": "proposal",
                "artifact_id": proposal_id,
                "content_draft_id": confirmed_id,
            }
            current_hash = page["readable_transcript"]["view_hash"]  # type: ignore[index]
            view_hash = view_hash or current_hash
            assert current_hash == view_hash
            paragraphs.extend(page["readable_transcript"]["paragraphs"])  # type: ignore[index]
            next_cursor = page["readable_transcript"]["next_cursor"]  # type: ignore[index]
            if next_cursor is None:
                break
            offset = next_cursor

        statuses = {paragraph["adoption_status"] for paragraph in paragraphs}
        assert "adopted" in statuses
        assert "partial" in statuses
        serialized = json.dumps(paragraphs, ensure_ascii=False)
        assert str(root) not in serialized
        assert str(tmp_path) not in serialized
        assert "locator" not in serialized
        assert "raw-asr" not in serialized
        assert review.token not in serialized

        status, error = _request(
            review,
            "POST",
            "/api/proposal-coverage",
            headers=_headers(review, write=True),
            body={"offset": 0, "limit": 1, "artifact_id": "proposal_other"},
        )
        assert status == 400
        assert error["error"]["code"] == "invalid_review_change"  # type: ignore[index]


def test_unrecorded_narration_blocks_workflow_proposal_without_mode_switch(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        blocks = _source_blocks() + [
            {
                "block_id": "block_narration",
                "kind": "narration",
                "text": "尚未录音的旁白。",
                "status": "draft",
                "recorded_refs": [],
            }
        ]
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": blocks},
        )
        assert status == 201
        candidate_id = created["content_draft_mutation"]["content_draft"][  # type: ignore[index]
            "content_draft_id"
        ]
        status, confirmed = _request(
            review,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(review, write=True),
            body={"content_draft_id": candidate_id},
        )
        assert status == 400
        assert confirmed["error"]["code"] == "workflow_not_ready"  # type: ignore[index]
        assert _request(review, "GET", "/api/workflow")[0] == 200
        status, retained = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200
        assert retained["candidate"]["candidate_id"] == candidate_id  # type: ignore[index]
        assert retained["candidate"]["confirmed_by_user"] is False  # type: ignore[index]
        assert ProjectStore(root).load().active_edit_version_id is None


def test_proposal_diff_uses_existing_service_when_active_decision_exists(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        _confirmed_id, proposal_id = _create_confirm_propose(review)
        status, adopted = _request(
            review,
            "POST",
            "/api/roughcut-adopt",
            headers=_headers(review, write=True),
            body={
                "basis_id": proposal_id,
                "expected_revision": ProjectStore(root).load().revision,
            },
        )
        assert status == 200, adopted
        decision_id = adopted["review"]["basis"]["id"]  # type: ignore[index]
    active_revision = ProjectStore(root).load().revision
    original = MultiSourceEditProposal.from_dict(
        json.loads(
            (root / "proposals" / f"{proposal_id}.json").read_text(
                encoding="utf-8"
            )
        )
    )
    refreshed_context = read_multi_source_agent_context(
        root,
        source_bindings=[
            binding.to_dict() for binding in original.source_bindings
        ],
        brief_id=original.brief_snapshot.brief_id,
        expected_revision=active_revision,
        offset=0,
        limit=1,
    )
    revision_proposal = replace(
        original,
        proposal_id="proposal_revision_diff",
        base_project_revision=active_revision,
        base_edit_version_id=decision_id,
        context_hash=refreshed_context.context_hash,
    )
    write_new_json(
        root / "proposals" / "proposal_revision_diff.json",
        revision_proposal.to_dict(),
    )

    with start_review_server(
        root,
        proposal_id=revision_proposal.proposal_id,
        static_root=_static(tmp_path),
    ) as review:
        status, diff = _request(review, "GET", "/api/proposal-diff")
        assert status == 200, diff
        assert diff["applicable"] is True  # type: ignore[index]
        assert diff["proposal_diff"]["proposal_id"] == revision_proposal.proposal_id  # type: ignore[index]
        assert diff["proposal_diff"]["base_edit_version_id"] == decision_id  # type: ignore[index]
        assert diff["project_revision"] == active_revision  # type: ignore[index]


def test_external_revision_makes_workflow_writes_read_only(tmp_path: Path) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
    ) as review:
        store = ProjectStore(root)
        current = store.load()
        store.save(replace(current, revision=current.revision + 1), expected_revision=current.revision)

        status, snapshot = _request(review, "GET", "/api/workflow")
        assert status == 200
        assert snapshot["session"]["read_only"] is True  # type: ignore[index]
        assert "project_revision" in snapshot["session"]["reasons"]  # type: ignore[index]
        status, error = _request(
            review,
            "POST",
            "/api/workflow/brief",
            headers=_headers(review, write=True),
            body={
                "theme": "不能写",
                "target_duration_ticks": 240_000,
                "focus": ["重点"],
                "allow_reorder": False,
            },
        )
        assert status == 409
        assert error["error"]["code"] == "stale_review"  # type: ignore[index]
        assert error["error"]["current_revision"] == 3  # type: ignore[index]


def test_workflow_security_rejects_malformed_method_and_expired_session_then_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _workflow_project(tmp_path)
    clock = [0.0]
    monkeypatch.setattr("roughcut.review.server.time.monotonic", lambda: clock[0])
    review = start_review_server(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],
        static_root=_static(tmp_path),
        token_ttl_seconds=1,
    )
    port = review.port
    try:
        status, malformed = _request(
            review,
            "POST",
            "/api/workflow/brief",
            headers=_headers(review, write=True),
            body=b"{",
        )
        assert status == 400
        assert malformed["error"]["code"] == "invalid_json"  # type: ignore[index]
        assert str(tmp_path) not in json.dumps(malformed)

        status, method_error = _request(
            review,
            "PUT",
            "/api/workflow/brief",
            headers=_headers(review, write=True),
            body={},
        )
        assert status == 405
        assert method_error["error"]["code"] == "method_not_allowed"  # type: ignore[index]

        status, wrong_type = _request(
            review,
            "POST",
            "/api/workflow/brief",
            headers=_headers(review, write=True),
            body={
                "theme": ["not", "text"],
                "target_duration_ticks": 240_000,
                "focus": ["重点"],
                "allow_reorder": False,
            },
        )
        assert status == 400
        assert wrong_type["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]

        def fail_without_leaking_path(_snapshot: object) -> object:
            raise OSError(str(tmp_path / "private" / "project.json"))

        monkeypatch.setattr(
            "roughcut.application.workflow_review.workflow_session_status",
            fail_without_leaking_path,
        )
        status, service_error = _request(review, "GET", "/api/workflow")
        assert status == 500
        assert service_error["error"]["code"] == "review_service_failed"  # type: ignore[index]
        assert str(tmp_path) not in json.dumps(service_error)

        clock[0] = 2.0
        status, expired = _request(review, "GET", "/api/workflow")
        assert status == 403
        assert expired["error"]["code"] == "forbidden"  # type: ignore[index]
    finally:
        review.close()

    assert review.thread.is_alive() is False
    probe = socket.socket()
    try:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        probe.close()


def test_gate5_external_submit_restarts_stale_branch_and_approves_current(
    tmp_path: Path,
) -> None:
    fixture = _workflow_project(tmp_path)
    root = Path(fixture["root"])
    static = _static(tmp_path)

    def artifact_ref_from_mutation(mutation: dict[str, object]) -> dict[str, object]:
        return {
            key: mutation[key]
            for key in ("artifact_id", "schema_version", "content_hash")
        }

    def external_submit(parent_ref: dict[str, object], action_id: str):
        project = ProjectStore(root).load()
        run = WorkflowStore(root).read_run("wfr_review")
        brief_ref = run.artifact_refs["brief"]
        assert brief_ref is not None
        context = read_multi_source_agent_context(
            root,
            source_bindings=fixture["bindings"],  # type: ignore[arg-type]
            brief_id=brief_ref.artifact_id,
            expected_revision=project.revision,
            offset=0,
            limit=20,
        )
        return workflow_action(
            root,
            "wfr_review",
            action_id,
            "submit_draft",
            {
                "schema_version": 1,
                "parent_draft_ref": parent_ref,
                "display_title": "Workflow 主题",
                "source_bindings": [
                    {
                        "source_id": binding.source_id,
                        "transcript_version_id": binding.transcript_version_id,
                    }
                    for binding in run.ordered_bindings
                ],
                "brief_ref": brief_ref.to_dict(),
                "context_hash": context.context_hash,
                "blocks": _source_blocks(),
                "scoped_mutable_block_ids": [],
            },
        )

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        static_root=static,
    ) as review:
        status, created = _request(
            review,
            "POST",
            "/api/workflow/content-drafts",
            headers=_headers(review, write=True),
            body={"blocks": _source_blocks()},
        )
        assert status == 201, created
        assert isinstance(created, dict)
        receipt = created["workflow_receipt"]
        assert isinstance(receipt, dict)
        anchor_mutation = receipt["mutation"]
        assert isinstance(anchor_mutation, dict)
        anchor_ref = artifact_ref_from_mutation(anchor_mutation)
        initial_status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert initial_status == 200, initial
        assert isinstance(initial, dict)
        initial_generation = initial["workspace"]["expected_checkpoint_ref"]["generation"]  # type: ignore[index]

    first_external = external_submit(anchor_ref, "act_gate5_external_child")
    assert first_external.receipt is not None
    first_mutation = first_external.receipt.mutation
    assert first_mutation is not None
    child_ref = artifact_ref_from_mutation(first_mutation.to_dict())

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        static_root=static,
    ) as restarted:
        status, reopened = _request(restarted, "GET", "/api/workflow/draft-editor")
        assert status == 200, reopened
        assert isinstance(reopened, dict)
        assert reopened["candidate"]["candidate_id"] == child_ref["artifact_id"]  # type: ignore[index]
        assert reopened["workspace"]["expected_checkpoint_ref"]["generation"] == initial_generation + 1  # type: ignore[index]

    run_after_child = WorkflowStore(root).read_run("wfr_review")
    anchor_after_child = run_after_child.artifact_refs["content_draft"]
    assert anchor_after_child is not None
    assert anchor_after_child.to_dict() == anchor_ref
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_review.json"
    checkpoint_before_stale = checkpoint_path.read_bytes()

    stale_external = external_submit(anchor_ref, "act_gate5_external_stale_sibling")
    assert stale_external.receipt is not None
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        static_root=static,
    ) as stale_review:
        status, stale = _request(stale_review, "GET", "/api/workflow/draft-editor")
        assert status == 409, stale
        assert isinstance(stale, dict)
        assert stale["error"]["code"] == "draft_workspace_stale"  # type: ignore[index]
    assert checkpoint_path.read_bytes() == checkpoint_before_stale

    second_external = external_submit(child_ref, "act_gate5_external_grandchild")
    assert second_external.receipt is not None
    grandchild = second_external.receipt.mutation
    assert grandchild is not None
    grandchild_id = grandchild.artifact_id

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        static_root=static,
    ) as review:
        status, reopened = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, reopened
        assert isinstance(reopened, dict)
        assert reopened["candidate"]["candidate_id"] == grandchild_id  # type: ignore[index]
        paragraph = reopened["paragraphs"][0]  # type: ignore[index]
        assert isinstance(paragraph, dict)
        status, edited = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "candidate_id": grandchild_id,
                    "operation": "delete",
                    "selection": {
                        "surface": "draft",
                        "anchor": {"paragraph_id": paragraph["paragraph_id"], "offset": 0},
                        "focus": {"paragraph_id": paragraph["paragraph_id"], "offset": 1},
                    },
                    "accept_degraded": True,
                },
            ),
        )
        assert status == 201, edited
        assert isinstance(edited, dict)
        edited_id = edited["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]

        status, undone = _request(
            review,
            "POST",
            "/api/workflow/draft-undo",
            headers=_headers(review, write=True),
            body={"candidate_id": edited_id},
        )
        assert status == 200, undone
        assert undone["draft_editor"]["candidate"]["candidate_id"] == grandchild_id  # type: ignore[index]
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_review.json"
    checkpoint_after_undo = checkpoint_path.read_bytes()

    with start_review_server(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        static_root=static,
    ) as after_undo:
        status, reopened_after_undo = _request(
            after_undo,
            "GET",
            "/api/workflow/draft-editor",
        )
        assert status == 200, reopened_after_undo
        assert isinstance(reopened_after_undo, dict)
        assert reopened_after_undo["candidate"]["candidate_id"] == grandchild_id  # type: ignore[index]
        assert checkpoint_path.read_bytes() == checkpoint_after_undo
        status, redone = _request(
            after_undo,
            "POST",
            "/api/workflow/draft-redo",
            headers=_headers(after_undo, write=True),
            body={"candidate_id": grandchild_id},
        )
        assert status == 200, redone
        assert redone["draft_editor"]["candidate"]["candidate_id"] == edited_id  # type: ignore[index]

    checkpoint_after_redo = checkpoint_path.read_bytes()
    with start_review_server(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        static_root=static,
    ) as after_redo:
        status, reopened_after_redo = _request(
            after_redo,
            "GET",
            "/api/workflow/draft-editor",
        )
        assert status == 200, reopened_after_redo
        assert isinstance(reopened_after_redo, dict)
        assert reopened_after_redo["candidate"]["candidate_id"] == edited_id  # type: ignore[index]
        assert checkpoint_path.read_bytes() == checkpoint_after_redo
        run_before_approve = WorkflowStore(root).read_run("wfr_review")
        assert run_before_approve.artifact_refs["content_draft"] is not None
        assert run_before_approve.artifact_refs["content_draft"].to_dict() == anchor_ref
        status, approved = _request(
            after_redo,
            "POST",
            "/api/workflow/content-draft-confirm",
            headers=_headers(after_redo, write=True),
            body={"content_draft_id": edited_id},
        )
        assert status == 200, approved
        assert approved["workflow_receipt"]["action"] == "approve_draft"  # type: ignore[index]

def _m3_4a_server(
    tmp_path: Path,
) -> tuple[Path, list[dict[str, object]], str, object]:
    """Build the M3.4 A synthetic project and start a workflow review server.

    The project mirrors the preserved acceptance site at commit `967bd322`
    (see `core/tests/review/m3_4_a_fixture.py`): 12 Chinese paragraphs across
    3 non-empty sections plus one empty section, a repeated phrase, emoji,
    editorial punctuation in canonical text, and one narration block.
    """
    from core.tests.review.m3_4_a_fixture import build_fixture

    project_path, bindings, draft_id = build_fixture(tmp_path)
    review = start_review_server(
        project_path,
        source_bindings=bindings,
        content_draft_id=draft_id,
        static_root=_static(tmp_path),
    )
    return project_path, bindings, draft_id, review


def _m3_4a_workspace_edit(review, operation: str, source: dict[str, object], target):
    return _request(
        review,
        "POST",
        "/api/workflow/draft-edit",
        headers=_headers(review, write=True),
        body=_workspace_body(review, {
            "schema_version": 2,
            "operation": operation,
            "accept_degraded": False,
            "source": source,
            "target": target,
        }),
    )


def _m3_4a_target(paragraph: dict[str, object]) -> dict[str, object]:
    return {
        "paragraph_id": paragraph["paragraph_id"],
        "block_id": _paragraph_block_id(paragraph),
        "utf16_offset": 0,
    }


def _m3_4a_insert_source_text(review, candidate_id: str, selected_text: str):
    status, window = _request(
        review,
        "POST",
        "/api/workflow/draft-transcript-window",
        headers=_headers(review, write=True),
        body={"candidate_id": candidate_id, "source_id": "src_b", "offset": 0, "limit": 48},
    )
    assert status == 200, window
    paragraph = next(
        item for item in window["paragraphs"] if "右侧原稿保持不变" in item["text"]
    )
    start = str(paragraph["text"]).index("保持不变")
    point = {"paragraph_id": paragraph["paragraph_id"], "offset_encoding": "utf16"}
    status, selection = _request(
        review,
        "POST",
        "/api/workflow/draft-selection-resolve",
        headers=_headers(review, write=True),
        body={
            "candidate_id": candidate_id,
            "surface": "source",
            "anchor": {**point, "offset": start},
            "focus": {**point, "offset": start + len(selected_text)},
        },
    )
    assert status == 200, selection
    resolution = selection["resolution"]
    refs = resolution["refs"]
    source = {
        "kind": "exact_source_refs",
        "source_id": "src_b",
        "transcript_version_id": "tr_b",
        "refs": [_without_canonical(ref) for ref in refs],
        "canonical_text": resolution["canonical_text"],
    }
    editor = _request(review, "GET", "/api/workflow/draft-editor")[1]
    status, inserted = _m3_4a_workspace_edit(
        review,
        "insert_source_refs",
        source,
        _m3_4a_target(editor["paragraphs"][0]),
    )
    assert status == 201, inserted
    return inserted, source


def test_m3_4_a_exact_source_insert_without_selected_punctuation(tmp_path: Path) -> None:
    project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        before_files = set((project_path / "content-drafts").glob("*.json"))
        inserted, source = _m3_4a_insert_source_text(review, draft_id, "保持不变")
        assert source["canonical_text"] == "保持不变" and source["refs"][0]["start_ticks"] == 11034 and source["refs"][0]["end_ticks"] == 22068
        assert inserted["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == 2 and inserted["operation"] == "insert_source_refs"
        assert len(set((project_path / "content-drafts").glob("*.json"))) == len(before_files) + 1
        blocks = inserted["draft_editor"]["blocks"]
        canonical = [block["canonical_text"] for block in blocks if block.get("kind") == "source_excerpt"]
        assert canonical.count("保持不变") == 1 and "保持不变，" not in canonical
    finally:
        review.close()


def test_m3_4_a_exact_source_insert_punctuation_move_reload_undo(tmp_path: Path) -> None:
    project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        before_files = set((project_path / "content-drafts").glob("*.json"))
        inserted, source = _m3_4a_insert_source_text(review, draft_id, "保持不变，")
        assert source["canonical_text"] == "保持不变，"
        assert source["refs"][0]["start_ticks"] == 11034 and source["refs"][0]["end_ticks"] == 24827
        after_insert_files = set((project_path / "content-drafts").glob("*.json"))
        assert len(after_insert_files) == len(before_files) + 1 and inserted["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == 2 and inserted["operation"] == "insert_source_refs"
        result_selection = inserted["result_selection"]
        response = result_selection["response"]
        resolution = response["resolution"]
        ref = resolution["refs"][0]
        assert ref["canonical_text"] == "保持不变，"
        assert ref["end_ticks"] == 24827
        assert ref["fine_unit_end_index"] == 9
        assert any(
            block.get("canonical_text") == "保持不变，" and block["refs"][0]["end_ticks"] == 24827
            for block in inserted["draft_editor"]["blocks"]
            if block.get("kind") == "source_excerpt"
        )

        current = _request(review, "GET", "/api/workflow/draft-editor")[1]
        current_candidate = current["candidate"]["candidate_id"]
        checkpoint = current["workspace"]["expected_checkpoint_ref"]
        tampered = {**source, "canonical_text": "保持不变"}
        target = current["paragraphs"][0]
        status, rejected = _m3_4a_workspace_edit(
            review,
            "insert_source_refs",
            tampered,
            _m3_4a_target(target),
        )
        assert status == 400, rejected
        assert rejected["error"]["code"] == "invalid_workflow_change"
        assert "canonical text is stale" in rejected["error"]["message"]
        assert set((project_path / "content-drafts").glob("*.json")) == after_insert_files
        unchanged = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert unchanged["workspace"]["expected_checkpoint_ref"] == checkpoint and unchanged["candidate"]["candidate_id"] == current_candidate
        source_for_move = _schema2_drop_source(unchanged, response)
        focus = source_for_move["display_range"]["focus"]
        source_paragraph = next(item for item in unchanged["paragraphs"] if item["paragraph_id"] == focus["paragraph_id"])
        focus["block_id"] = next(run["block_id"] for run in source_paragraph["source_runs"] if run["start_offset"] == focus["utf16_offset"])
        target = next(
            paragraph
            for paragraph in unchanged["paragraphs"]
            if str(paragraph.get("text", "")).startswith("重复原话")
        )
        status, moved = _m3_4a_workspace_edit(
            review,
            "move_selection",
            source_for_move,
            _m3_4a_target(target),
        )
        assert status == 201, moved
        moved_id = moved["draft_editor"]["candidate"]["candidate_id"]
        reloaded = _request(review, "GET", "/api/workflow/draft-editor")[1]
        blocks = reloaded["blocks"]
        source_blocks = [block for block in blocks if block.get("kind") == "source_excerpt"]
        selected = [block for block in source_blocks if block.get("canonical_text") == "保持不变，"]
        assert len(selected) == 1 and selected[0]["canonical_text"].count("，") == 1
        assert any(
            ref.get("start_ticks") == 11034 and ref.get("end_ticks") == 24827
            for ref in selected[0].get("refs", [])
        )
        status, undone = _request(review, "POST", "/api/workflow/draft-undo",
                                  headers=_headers(review, write=True), body={"candidate_id": moved_id})
        assert status == 200, undone
        assert undone["draft_editor"]["candidate"]["candidate_id"] == current_candidate
    finally:
        review.close()


def test_m3_4_a_plain_paragraph_selection_move_is_not_a_display_ambiguity(
    tmp_path: Path,
) -> None:
    """Preserved acceptance failure: canonical-only paragraph must not 400.

    The preserved site (`/private/tmp/m3-4-a-false-stale-acceptance-QF3Wan/`,
    browser-events.jsonl records 43-48) shows a `move_selection` of a pure
    speech selection (canonical「请大家重点」, Draft UTF-16 6→11) failing with
    `draft editor selection cannot uniquely retain display punctuation`
    although the candidate had no `display_text` anywhere.  The resolve
    response (record 43) is asserted below verbatim.
    """
    project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        assert initial["candidate"]["candidate_id"] == draft_id  # type: ignore[index]
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        block_00 = next(
            item
            for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict)
        block_00_text = str(block_00["text"])
        assert "请大家重点" in block_00_text
        block_01 = next(
            item
            for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_01"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_01, dict)

        # --- record 41: selection-resolve request (UTF-16 6->11) ---
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": draft_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 6,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 11,
                    "offset_encoding": "utf16",
                },
            },
        )
        # --- record 43: resolve 200 with the preserved response ---
        assert status == 200, selection
        assert isinstance(selection, dict)
        assert selection["candidate_id"] == draft_id  # type: ignore[index]
        resolution = selection["resolution"]
        assert isinstance(resolution, dict)
        assert resolution["canonical_text"] == "请大家重点"  # type: ignore[index]
        assert resolution["degraded"] is False  # type: ignore[index]
        assert resolution["adjusted"] is False  # type: ignore[index]
        assert resolution["direction"] == "forward"  # type: ignore[index]
        refs = resolution["refs"]
        assert isinstance(refs, list) and len(refs) == 1
        assert refs[0]["segment_id"] == "seg_src_a_00"  # type: ignore[index]
        assert refs[0]["start_ticks"] == 19_200  # type: ignore[index]
        assert refs[0]["end_ticks"] == 35_200  # type: ignore[index]
        assert refs[0]["canonical_text"] == "请大家重点"  # type: ignore[index]
        display_range = selection["display_range"]
        assert isinstance(display_range, dict)
        assert display_range["anchor"]["paragraph_id"] == block_00["paragraph_id"]  # type: ignore[index]
        assert display_range["anchor"]["utf16_offset"] == 6  # type: ignore[index]
        assert display_range["focus"]["utf16_offset"] == 11  # type: ignore[index]
        correspondence = selection["correspondence_groups"]
        assert isinstance(correspondence, list) and len(correspondence) == 1
        assert correspondence[0]["start_offset"] == 6  # type: ignore[index]
        assert correspondence[0]["end_offset"] == 11  # type: ignore[index]
        assert correspondence[0]["source_id"] == "src_a"  # type: ignore[index]
        assert isinstance(selection["resolution_hash"], str)  # type: ignore[index]

        # --- records 45/47: the complete schema-2 move envelope ---
        # On the preserved acceptance site this request failed with the 400
        # below (browser-events.jsonl records 46/48).  The move must succeed
        # instead: the source paragraph carries no editorial display text, so
        # there is no display punctuation whose ownership could be ambiguous.
        target = {
            "paragraph_id": block_01["paragraph_id"],
            "block_id": "block_01",
            "utf16_offset": 9,
        }
        workspace = initial["workspace"]
        assert isinstance(workspace, dict)
        checkpoint_ref = workspace["expected_checkpoint_ref"]
        assert isinstance(checkpoint_ref, dict)
        body = {
            "schema_version": 2,
            "operation": "move_selection",
            "accept_degraded": False,
            "source": _schema2_drop_source(initial, selection),
            "target": target,
        }
        before_files = sorted((project_path / "content-drafts").glob("*.json"))
        before_generation = checkpoint_ref["generation"]
        assert isinstance(before_generation, int)
        envelope = {
            "operation_id": f"dwop_{before_generation}_{secrets.token_hex(16)}",
            "expected_checkpoint_ref": checkpoint_ref,
            "expected_current_candidate_ref": workspace[
                "expected_current_candidate_ref"
            ],
        }
        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={**body, **envelope},
        )
        if status == 400:
            # Preserved failure signature (records 46/48): a canonical-only
            # paragraph must never be reported as a display ambiguity.
            assert isinstance(moved, dict)
            assert moved["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
            assert (  # type: ignore[index]
                moved["error"]["message"]
                == "draft editor selection cannot uniquely retain display punctuation"
            )
            assert sorted((project_path / "content-drafts").glob("*.json")) == before_files
            baseline_revision = ProjectStore(project_path).load().revision
            after = _request(review, "GET", "/api/workflow/draft-editor")[1]
            assert isinstance(after, dict)
            assert after["candidate"]["candidate_id"] == draft_id  # type: ignore[index]
            assert after["workspace"] == workspace  # type: ignore[index]
            assert ProjectStore(project_path).load().revision == baseline_revision
            pytest.fail(
                "canonical-only paragraph selection raised the preserved "
                "display punctuation 400; expected a successful move"
            )
        assert status == 201, moved
        assert isinstance(moved, dict)
        assert moved["schema_version"] == 2  # type: ignore[index]
        assert moved["operation"] == "move_selection"  # type: ignore[index]
        child_id = moved["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert child_id != draft_id
        assert moved["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == before_generation + 1  # type: ignore[index]
        assert len(sorted((project_path / "content-drafts").glob("*.json"))) == len(
            before_files
        ) + 1
        _assert_edit_timing(moved, "move_selection")  # type: ignore[arg-type]
        # Fixed judgment principles: the split fragments' non-punctuation
        # sequences equal the canonical ref texts, and the display-only
        # punctuation stays with the display text without producing refs,
        # ticks, or media ranges.
        blocks = moved["draft_editor"]["blocks"]
        assert isinstance(blocks, list)
        source_blocks = [b for b in blocks if isinstance(b, dict) and b.get("kind") == "source_excerpt"]
        moved_left = next(
            b for b in source_blocks if b.get("canonical_text") == "主持人说"
        )
        assert moved_left["display_text"] == "主持人说：‘"  # type: ignore[index]
        moved_right = next(
            b for b in source_blocks if b.get("canonical_text") == "内容然后再讨论下一步安排"
        )
        assert moved_right["display_text"] == "内容然后再讨论下一步安排。’"  # type: ignore[index]
        selected = next(
            b for b in source_blocks if b.get("canonical_text") == "请大家重点"
        )
        assert selected["refs"][0]["start_ticks"] == 19_200  # type: ignore[index]
        assert selected["refs"][0]["end_ticks"] == 35_200  # type: ignore[index]
        all_ticks = [ref["start_ticks"] for b in source_blocks for ref in b["refs"]]  # type: ignore[index]
        assert min(all_ticks) >= 0
        assert max(all_ticks) <= 1_180_000
        # exactly one child artifact and one committed operation
        checkpoint = json.loads(
            (project_path / "workflow" / "draft-workspaces" / "wfr_p1.json").read_text(
                encoding="utf-8"
            )
        )
        assert checkpoint["generation"] == 2
        assert checkpoint["last_commit"]["operation"] == "edit_move"
    finally:
        review.close()


def test_m3_4_a_caret_path_within_display_text_paragraph_succeeds(
    tmp_path: Path,
) -> None:
    """Preserved acceptance failure shape (records 63-66): caret-targeted move.

    The preserved site showed a `move_selection` with selection UTF-16 7->9
    (「大家」) in block_00 and the caret at the same paragraph UTF-16 12
    failing with `draft editor caret cannot uniquely retain display
    punctuation`.  With the display-offset fix the same shape must succeed
    with exactly one child and one operation, and the split fragments must
    keep the display punctuation attached to their own sides.
    """
    project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        block_00 = next(
            item
            for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict)

        # Preserved shape: selection 7->9 (「大家」), caret same paragraph 12
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": draft_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 7,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 9,
                    "offset_encoding": "utf16",
                },
            },
        )
        assert status == 200, selection
        assert isinstance(selection, dict)
        resolution = selection["resolution"]
        assert isinstance(resolution, dict)
        assert resolution["canonical_text"] == "大家"  # type: ignore[index]
        refs = resolution["refs"]
        assert isinstance(refs, list) and len(refs) == 1
        assert refs[0]["start_ticks"] == 22_400  # type: ignore[index]
        assert refs[0]["end_ticks"] == 28_800  # type: ignore[index]
        body = {
            "schema_version": 2,
            "operation": "move_selection",
            "accept_degraded": False,
            "source": _schema2_drop_source(initial, selection),
            "target": {
                "paragraph_id": block_00["paragraph_id"],
                "block_id": "block_00",
                "utf16_offset": 12,
            },
        }
        before_files = sorted((project_path / "content-drafts").glob("*.json"))
        before_generation = initial["workspace"]["expected_checkpoint_ref"]["generation"]  # type: ignore[index]
        assert isinstance(before_generation, int)
        envelope = {
            "operation_id": f"dwop_{before_generation}_{secrets.token_hex(16)}",
            "expected_checkpoint_ref": initial["workspace"][
                "expected_checkpoint_ref"
            ],
            "expected_current_candidate_ref": initial["workspace"][
                "expected_current_candidate_ref"
            ],
        }
        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body={**body, **envelope},
        )
        if status == 400:
            # Preserved failure signature (records 64/66): the caret path must
            # be the one that refuses, with the caret-form message.
            assert isinstance(moved, dict)
            assert moved["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
            assert (  # type: ignore[index]
                moved["error"]["message"]
                == "draft editor caret cannot uniquely retain display punctuation"
            )
            assert sorted((project_path / "content-drafts").glob("*.json")) == before_files
            after = _request(review, "GET", "/api/workflow/draft-editor")[1]
            assert isinstance(after, dict)
            assert after["workspace"] == initial["workspace"]  # type: ignore[index]
            pytest.fail(
                "same-paragraph caret selection raised the preserved caret "
                "400; expected a successful move"
            )
        assert status == 201, moved
        assert isinstance(moved, dict)
        assert moved["schema_version"] == 2  # type: ignore[index]
        assert moved["operation"] == "move_selection"  # type: ignore[index]
        child_id = moved["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert child_id != draft_id
        assert moved["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == before_generation + 1  # type: ignore[index]
        assert len(sorted((project_path / "content-drafts").glob("*.json"))) == len(
            before_files
        ) + 1
        blocks = moved["draft_editor"]["blocks"]
        assert isinstance(blocks, list)
        source_blocks = [b for b in blocks if isinstance(b, dict) and b.get("kind") == "source_excerpt"]
        # block_00 splits around the selection and the caret boundary; the
        # moved selection stays an exact ref with unchanged ticks.
        selected = next(
            b for b in source_blocks if b.get("canonical_text") == "大家"
        )
        assert selected["refs"][0]["start_ticks"] == 22_400  # type: ignore[index]
        assert selected["refs"][0]["end_ticks"] == 28_800  # type: ignore[index]
        assert selected["refs"][0]["segment_id"] == "seg_src_a_00"  # type: ignore[index]
        # split fragment display attribution: fragments whose display equals
        # their canonical text carry no display_text field; the fragment that
        # keeps the trailing display-only punctuation must keep it in
        # display_text without generating refs, ticks, or media ranges.
        left_piece = next(
            b for b in source_blocks if b.get("canonical_text") == "主持人说：‘请"
        )
        assert left_piece.get("display_text") is None  # type: ignore[index]
        mid_piece = next(
            b for b in source_blocks if b.get("canonical_text") == "重点内"
        )
        assert mid_piece.get("display_text") is None  # type: ignore[index]
        right_piece = next(
            b for b in source_blocks if b.get("canonical_text") == "容然后再讨论下一步安排"
        )
        assert right_piece["display_text"] == "容然后再讨论下一步安排。’"  # type: ignore[index]
        assert right_piece["refs"][0]["start_ticks"] == 38_400  # type: ignore[index]
        # trailing display-only 。’ generates no refs or ticks: the ref stops
        # at the last non-punctuation character (排)
        assert right_piece["refs"][0]["end_ticks"] == 73_600  # type: ignore[index]
    finally:
        review.close()


def test_m3_4_a_genuine_display_ambiguity_still_fails_closed(
    tmp_path: Path,
) -> None:
    """A caret that would orphan a display-only punctuation still fails closed.

    When display text inserts a punctuation character that a boundary caret
    cannot uniquely retain (here: an opening bracket at the block start, with
    the caret at the canonical start), the editor must refuse the move with
    zero writes instead of silently dropping the punctuation.
    """
    from roughcut.application.content_drafts import create_content_draft_editor_child
    from roughcut.domain.content_draft import SourceExcerptBlock

    project_path, bindings, draft_id, review = _m3_4a_server(tmp_path)
    snapshot = draft_editor_module.load_draft_editor_snapshot(
        project_path,
        source_bindings=bindings,
        content_draft_id=draft_id,
    )
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        block_00 = next(
            item
            for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict)
        block_00_text = str(block_00["text"])
        display_text = "（" + block_00_text
        child = create_content_draft_editor_child(
            project_path,
            parent=snapshot.candidate,
            blocks=tuple(
                replace(block, display_text=display_text)
                if isinstance(block, SourceExcerptBlock)
                and block.block_id == "block_00"
                else block
                for block in snapshot.candidate.blocks
            ),
            expected_revision=ProjectStore(project_path).load().revision,
        )
        display_draft_id = child.content_draft.content_draft_id
        status, display_editor = _request(
            review,
            "POST",
            "/api/workflow/draft-candidate-select",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "parent_candidate_id": draft_id,
                    "child_candidate_id": display_draft_id,
                },
            ),
        )
        assert status == 200, display_editor
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        assert isinstance(editor, dict)
        display_block_00 = next(
            item
            for item in editor["paragraphs"]  # type: ignore[index]
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(display_block_00, dict)
        assert str(display_block_00["text"]).startswith("（")
        # selection inside block_00 (canonical 2..6 = 持人)
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": display_draft_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": display_block_00["paragraph_id"],
                    "offset": 2,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": display_block_00["paragraph_id"],
                    "offset": 6,
                    "offset_encoding": "utf16",
                },
            },
        )
        assert status == 200, selection
        assert isinstance(selection, dict)
        # caret at the canonical start of the same block would orphan the
        # leading display-only bracket: the bracket cannot uniquely follow
        # either side of the caret.
        body = {
            "schema_version": 2,
            "operation": "move_selection",
            "accept_degraded": False,
            "source": _schema2_drop_source(editor, selection),
            "target": {
                "paragraph_id": display_block_00["paragraph_id"],
                "block_id": "block_00",
                "utf16_offset": 0,
            },
        }
        before_files = sorted((project_path / "content-drafts").glob("*.json"))
        before_generation = editor["workspace"]["expected_checkpoint_ref"]["generation"]  # type: ignore[index]
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=_workspace_body(review, body),
        )
        assert status == 400, rejected
        assert isinstance(rejected, dict)
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert "punctuation" in str(rejected["error"]["message"])  # type: ignore[index]
        assert sorted((project_path / "content-drafts").glob("*.json")) == before_files
        unchanged = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(unchanged, dict)
        assert unchanged["workspace"] == editor["workspace"]  # type: ignore[index]
        assert unchanged["workspace"]["expected_checkpoint_ref"]["generation"] == before_generation  # type: ignore[index]
    finally:
        review.close()


def test_m3_4_a_occurrence_boundary_carets_insert_without_punctuation_400(
    tmp_path: Path,
) -> None:
    """A clean (no editorial punctuation) caret before an English word inserts.

    Regression for the occurrence-boundary 400: `_display_parts_for_caret`
    used the left neighbor's non-punctuation length to cut the current
    atom's canonical, so a caret inside the atom (offset 10/11 of
    "重复原话用于验证既有 occurrence，保留人工标点。") split at the wrong
    point and was rejected with `caret cannot uniquely retain display
    punctuation`.  Both offsets must now insert successfully.
    """
    for caret_offset in (10, 11):
        project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path / f"offset-{caret_offset}")
        try:
            status, initial = _request(review, "GET", "/api/workflow/draft-editor")
            assert status == 200, initial
            assert isinstance(initial, dict)
            paragraphs = initial["paragraphs"]
            assert isinstance(paragraphs, list)
            occurrence_para = next(
                item
                for item in paragraphs
                if isinstance(item, dict) and "occurrence" in str(item.get("text"))
            )
            assert isinstance(occurrence_para, dict)
            assert str(occurrence_para["text"]).index("occurrence") == 11

            status, selection = _request(
                review,
                "POST",
                "/api/workflow/draft-selection-resolve",
                headers=_headers(review, write=True),
                body={
                    "candidate_id": draft_id,
                    "surface": "draft",
                    "anchor": {
                        "paragraph_id": occurrence_para["paragraph_id"],
                        "offset": 2,
                        "offset_encoding": "utf16",
                    },
                    "focus": {
                        "paragraph_id": occurrence_para["paragraph_id"],
                        "offset": 8,
                        "offset_encoding": "utf16",
                    },
                },
            )
            assert status == 200, selection
            assert isinstance(selection, dict)
            before_files = sorted((project_path / "content-drafts").glob("*.json"))
            body = {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": _schema2_drop_source(initial, selection),
                "target": {
                    "paragraph_id": occurrence_para["paragraph_id"],
                    "block_id": "block_01",
                    "utf16_offset": caret_offset,
                },
            }
            status, moved = _request(
                review,
                "POST",
                "/api/workflow/draft-edit",
                headers=_headers(review, write=True),
                body=_workspace_body(review, body),
            )
            assert status == 201, moved
            assert isinstance(moved, dict)
            assert moved["schema_version"] == 2  # type: ignore[index]
            assert moved["operation"] == "move_selection"  # type: ignore[index]
            assert len(sorted((project_path / "content-drafts").glob("*.json"))) == len(
                before_files
            ) + 1
        finally:
            review.close()


def _m3_4a_schema2_move(
    review,
    editor: dict[str, object],
    *,
    anchor_para: dict[str, object],
    anchor_utf16: int,
    focus_utf16: int,
    source_block_id: str,
    target_para: dict[str, object],
    target_block_id: str,
    target_utf16: int,
) -> tuple[int, dict[str, object]]:
    """Run one schema-2 move_selection and return (status, payload)."""
    status, selection = _request(
        review,
        "POST",
        "/api/workflow/draft-selection-resolve",
        headers=_headers(review, write=True),
        body={
            "candidate_id": editor["candidate"]["candidate_id"],
            "surface": "draft",
            "anchor": {
                "paragraph_id": anchor_para["paragraph_id"],
                "offset": anchor_utf16,
                "offset_encoding": "utf16",
            },
            "focus": {
                "paragraph_id": anchor_para["paragraph_id"],
                "offset": focus_utf16,
                "offset_encoding": "utf16",
            },
        },
    )
    assert status == 200, selection
    assert isinstance(selection, dict)
    body = _workspace_body(
        review,
        {
            "schema_version": 2,
            "operation": "move_selection",
            "accept_degraded": bool(selection["resolution"]["degraded"]),
            "source": _schema2_drop_source(editor, selection),
            "target": {
                "paragraph_id": target_para["paragraph_id"],
                "block_id": target_block_id,
                "utf16_offset": target_utf16,
            },
        },
    )
    return _request(
        review,
        "POST",
        "/api/workflow/draft-edit",
        headers=_headers(review, write=True),
        body=body,
    )


def _assert_result_selection_shape(result_selection: dict[str, object]) -> None:
    assert set(result_selection) == {"surface", "request", "response", "accepted_degraded"}
    assert result_selection["surface"] == "draft"
    assert result_selection["accepted_degraded"] is True
    request = result_selection["request"]
    assert isinstance(request, dict)
    assert set(request) == {"anchor", "focus"}
    for point in (request["anchor"], request["focus"]):
        assert isinstance(point, dict)
        assert set(point) == {"paragraph_id", "offset", "offset_encoding"}
        assert point["offset_encoding"] == "utf16"
    response = result_selection["response"]
    assert isinstance(response, dict)
    assert response["surface"] == "draft"
    resolution = response["resolution"]
    assert isinstance(resolution, dict)
    assert resolution["direction"] == "forward"
    assert resolution["adjusted"] is False
    for caret_key in ("start_caret", "end_caret"):
        caret = resolution[caret_key]
        assert isinstance(caret, dict)
        assert caret["degraded"] is False
        assert caret["paragraph_id"].startswith("paragraph_")
    assert isinstance(response["resolution_hash"], str)
    assert len(response["resolution_hash"]) == 64
    for range_key in ("display_range", "resolved_display_range"):
        display = response[range_key]
        assert isinstance(display, dict)
        for point in (display["anchor"], display["focus"]):
            assert isinstance(point, dict)
            assert set(point) == {"paragraph_id", "character_offset", "utf16_offset"}
    correspondence = response["correspondence_groups"]
    assert isinstance(correspondence, list) and correspondence
    assert isinstance(response["candidate_id"], str)


def test_m3_4_a_result_selection_move_wire_and_chained_second_operation(
    tmp_path: Path,
) -> None:
    """result_selection full wire for move, reusable for the next operation."""
    _project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        block_00 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        block_01 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_01"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict) and isinstance(block_01, dict)
        # resolve the selection and keep the exact schema-2 envelope so the
        # same request can be replayed for the readback case
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": draft_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 6,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 11,
                    "offset_encoding": "utf16",
                },
            },
        )
        assert status == 200, selection
        assert isinstance(selection, dict)
        body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": _schema2_drop_source(initial, selection),
                "target": {
                    "paragraph_id": block_01["paragraph_id"],
                    "block_id": "block_01",
                    "utf16_offset": 9,
                },
            },
        )
        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, moved
        _assert_result_selection_shape(moved["result_selection"])  # type: ignore[index]
        result_selection = moved["result_selection"]
        assert isinstance(result_selection, dict)
        response = result_selection["response"]
        assert isinstance(response, dict)
        resolution = response["resolution"]
        assert isinstance(resolution, dict)
        assert resolution["canonical_text"] == "请大家重点"
        refs = resolution["refs"]
        assert isinstance(refs, list) and len(refs) == 1
        assert refs[0]["segment_id"] == "seg_src_a_00"  # type: ignore[index]
        assert refs[0]["start_ticks"] == 19_200  # type: ignore[index]
        assert refs[0]["end_ticks"] == 35_200  # type: ignore[index]
        start_caret = resolution["start_caret"]
        end_caret = resolution["end_caret"]
        assert isinstance(start_caret, dict) and isinstance(end_caret, dict)
        assert start_caret["character_offset"] == 6  # type: ignore[index]
        assert end_caret["character_offset"] == 11  # type: ignore[index]
        correspondence = response["correspondence_groups"]
        assert isinstance(correspondence, list) and len(correspondence) == 1
        assert correspondence[0]["start_offset"] == 6  # type: ignore[index]
        assert correspondence[0]["end_offset"] == 11  # type: ignore[index]
        assert correspondence[0]["source_id"] == "src_a"  # type: ignore[index]
        # request display identity is Draft, carets are Transcript identity
        request = result_selection["request"]
        assert isinstance(request, dict)
        assert request["anchor"]["paragraph_id"].startswith("draft_paragraph_")  # type: ignore[index]
        assert start_caret["paragraph_id"].startswith("paragraph_")  # type: ignore[index]
        assert request["anchor"]["paragraph_id"] != start_caret["paragraph_id"]  # type: ignore[index]

        # replaying the identical envelope is a readback: 201 without
        # result_selection (readback=true must omit the wire)
        status, readback = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, readback
        assert "result_selection" not in readback
        assert readback["draft_editor"]["candidate"] == moved["draft_editor"]["candidate"]  # type: ignore[index]
        assert readback["draft_editor"]["workspace"] == moved["draft_editor"]["workspace"]  # type: ignore[index]

        # the returned result_selection must be immediately reusable: delete
        # the moved selection at its new position via a second operation
        child_id = moved["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        status, editor_after = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor_after
        assert isinstance(editor_after, dict)
        delete_selection = {
            "surface": "draft",
            "anchor": {
                "paragraph_id": request["anchor"]["paragraph_id"],
                "offset": request["anchor"]["offset"],
                "offset_encoding": "utf16",
            },
            "focus": {
                "paragraph_id": request["focus"]["paragraph_id"],
                "offset": request["focus"]["offset"],
                "offset_encoding": "utf16",
            },
        }
        delete_body = _workspace_body(
            review,
            {
                "candidate_id": child_id,
                "operation": "delete",
                "selection": delete_selection,
                "accept_degraded": False,
            },
        )
        status, deleted = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=delete_body,
        )
        assert status == 201, deleted
        assert deleted["draft_editor"]["candidate"]["candidate_id"] != child_id  # type: ignore[index]
        assert "result_selection" not in deleted
        deleted_blocks = deleted["draft_editor"]["blocks"]
        assert isinstance(deleted_blocks, list)
        assert not any(
            isinstance(block, dict)
            and block.get("canonical_text") == "请大家重点"
            for block in deleted_blocks
        )
    finally:
        review.close()


def test_m3_4_a_result_selection_insert_wire_with_emoji_and_fine_units(
    tmp_path: Path,
) -> None:
    """insert result_selection with emoji surrogate and fine-unit extension."""
    _project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        block_02 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_02"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_02, dict)
        # select the whole emoji segment (seg_src_a_03) in the source window
        status, window = _request(
            review,
            "POST",
            "/api/workflow/draft-transcript-window",
            headers=_headers(review, write=True),
            body={
                "candidate_id": draft_id,
                "source_id": "src_a",
                "offset": 0,
                "limit": 48,
            },
        )
        assert status == 200, window
        assert isinstance(window, dict)
        emoji_para = window["paragraphs"][0]
        assert isinstance(emoji_para, dict)
        emoji_text = str(emoji_para["text"])
        emoji_codepoint = emoji_text.find("😀")
        assert emoji_codepoint > 0
        segment_03_ref = next(
            ref for ref in emoji_para["refs"]
            if isinstance(ref, dict) and ref.get("segment_id") == "seg_src_a_03"
        )
        assert isinstance(segment_03_ref, dict)
        # segment 03 is the 4th paragraph; its codepoint start is the sum of
        # the three preceding texts plus three newline separators
        preceding = [
            "主持人说：‘请大家重点内容然后再讨论下一步安排。’",
            "重复原话用于验证既有 occurrence，保留人工标点。",
            "这一段完整中文正文用于提供稳定的起始上下文。",
        ]
        start_codepoint = sum(len(text) for text in preceding) + 3
        segment_text = str(segment_03_ref["text"]) if "text" in segment_03_ref else "第四段正文包含主持人的补充说明和一个😀表情。"
        end_codepoint = start_codepoint + len(segment_text)
        status, source_selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": draft_id,
                "surface": "source",
                "anchor": {
                    "paragraph_id": emoji_para["paragraph_id"],
                    "offset": start_codepoint,
                    "offset_encoding": "codepoint",
                },
                "focus": {
                    "paragraph_id": emoji_para["paragraph_id"],
                    "offset": end_codepoint,
                    "offset_encoding": "codepoint",
                },
            },
        )
        assert status == 200, source_selection
        assert isinstance(source_selection, dict)
        resolution = source_selection["resolution"]
        assert isinstance(resolution, dict)
        assert "😀" in str(resolution["canonical_text"])
        refs = resolution["refs"]
        assert isinstance(refs, list) and refs
        assert refs[0]["segment_id"] == "seg_src_a_03"  # type: ignore[index]
        assert refs[0]["start_ticks"] == 300_000  # type: ignore[index]
        assert refs[0]["end_ticks"] == 380_000  # type: ignore[index]
        source = {
            "kind": "exact_source_refs",
            "source_id": refs[0]["source_id"],
            "transcript_version_id": refs[0]["transcript_version_id"],
            "refs": [_without_canonical(ref) for ref in refs],
            "canonical_text": resolution["canonical_text"],
        }
        body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "insert_source_refs",
                "accept_degraded": bool(resolution["degraded"]),
                "source": source,
                "target": {
                    "paragraph_id": block_02["paragraph_id"],
                    "block_id": "block_02",
                    "utf16_offset": 0,
                },
            },
        )
        status, inserted = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, inserted
        assert "result_highlight" not in inserted
        result_selection = inserted["result_selection"]
        assert isinstance(result_selection, dict)
        _assert_result_selection_shape(result_selection)
        response = result_selection["response"]
        assert isinstance(response, dict)
        inserted_resolution = response["resolution"]
        assert isinstance(inserted_resolution, dict)
        assert inserted_resolution["canonical_text"] == str(resolution["canonical_text"])
        # emoji surrogate pair survives intact in the resolved canonical text
        assert "😀" in str(inserted_resolution["canonical_text"])
        inserted_refs = inserted_resolution["refs"]
        assert isinstance(inserted_refs, list)
        assert len(inserted_refs) == len(refs)
        assert inserted_refs[0]["segment_id"] == "seg_src_a_03"  # type: ignore[index]
        assert inserted_refs[0]["start_ticks"] == 300_000  # type: ignore[index]
        assert inserted_refs[0]["end_ticks"] == 380_000  # type: ignore[index]
        display = response["display_range"]
        assert isinstance(display, dict)
        assert display["anchor"]["character_offset"] == 0  # type: ignore[index]
        # request is Draft identity, correspondence is Transcript identity
        request = result_selection["request"]
        assert isinstance(request, dict)
        assert request["anchor"]["paragraph_id"].startswith("draft_paragraph_")  # type: ignore[index]
        correspondence = response["correspondence_groups"]
        assert isinstance(correspondence, list) and correspondence
        assert correspondence[0]["paragraph_id"].startswith("paragraph_")  # type: ignore[index]
        assert correspondence[0]["source_id"] == "src_a"  # type: ignore[index]
    finally:
        review.close()


def test_m3_4_a_result_selection_repeated_phrase_binds_new_occurrence(
    tmp_path: Path,
) -> None:
    """Repeated speech selects the new occurrence bound to the new child."""
    _project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        block_00 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        block_01 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_01"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict) and isinstance(block_01, dict)
        # the phrase 请大家重点 appears in block_00 and block_08; resolve in
        # block_00 and move it, then the result must bind the new child
        status, moved = _m3_4a_schema2_move(
            review,
            initial,
            anchor_para=block_00,
            anchor_utf16=6,
            focus_utf16=11,
            source_block_id="block_00",
            target_para=block_01,
            target_block_id="block_01",
            target_utf16=0,
        )
        assert status == 201, moved
        result_selection = moved["result_selection"]
        assert isinstance(result_selection, dict)
        response = result_selection["response"]
        assert isinstance(response, dict)
        # resolution_hash binds the new child candidate identity
        child_id = moved["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert response["candidate_id"] == child_id
        assert response["candidate_id"] != draft_id
        resolution = response["resolution"]
        assert isinstance(resolution, dict)
        refs = resolution["refs"]
        assert isinstance(refs, list) and len(refs) == 1
        # the moved occurrence stays the exact block_00 refs (22400-35200
        # would be block_08; the new occurrence is the block_00 segment)
        assert refs[0]["segment_id"] == "seg_src_a_00"  # type: ignore[index]
        assert refs[0]["start_ticks"] == 19_200  # type: ignore[index]
        assert refs[0]["end_ticks"] == 35_200  # type: ignore[index]
        assert isinstance(response["resolution_hash"], str)
    finally:
        review.close()


def test_m3_4_a_result_selection_multiparagraph_cross_section_no_heading(
    tmp_path: Path,
) -> None:
    """Multi-paragraph cross-section move omits heading content."""
    _project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        # select across block_04 (section 2) and block_06 (section 2) via a
        # caret-dragged range spanning two paragraphs
        block_04 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_04"
                for run in item.get("source_runs", [])
            )
        )
        block_06 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_06"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_04, dict) and isinstance(block_06, dict)
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": draft_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": block_04["paragraph_id"],
                    "offset": 0,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": block_06["paragraph_id"],
                    "offset": len(str(block_06["text"])),
                    "offset_encoding": "utf16",
                },
            },
        )
        assert status == 200, selection
        assert isinstance(selection, dict)
        resolution = selection["resolution"]
        assert isinstance(resolution, dict)
        refs = resolution["refs"]
        assert isinstance(refs, list)
        assert len(refs) == 3  # blocks 04, 05, 06
        assert refs[0]["segment_id"] == "seg_src_a_04"  # type: ignore[index]
        assert refs[-1]["segment_id"] == "seg_src_a_06"  # type: ignore[index]
        assert "第一章" not in str(resolution["canonical_text"])
        # move onto block_11 (section 3) - cross-section destination
        block_11 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_11"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_11, dict)
        source = _schema2_drop_source(initial, selection)
        body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": bool(resolution["degraded"]),
                "source": source,
                "target": {
                    "paragraph_id": block_11["paragraph_id"],
                    "block_id": "block_11",
                    "utf16_offset": 0,
                },
            },
        )
        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, moved
        result_selection = moved["result_selection"]
        assert isinstance(result_selection, dict)
        _assert_result_selection_shape(result_selection)
        response = result_selection["response"]
        assert isinstance(response, dict)
        moved_resolution = response["resolution"]
        assert isinstance(moved_resolution, dict)
        assert len(moved_resolution["refs"]) == 3  # type: ignore[index]
        # no heading block inside the result
        assert "第一章" not in str(moved_resolution["canonical_text"])
        assert "第二章" not in str(moved_resolution["canonical_text"])
        display = response["display_range"]
        assert isinstance(display, dict)
        assert display["anchor"]["paragraph_id"].startswith("draft_paragraph_")  # type: ignore[index]
    finally:
        review.close()


def test_m3_4_a_result_selection_explicit_and_unselected_punctuation(
    tmp_path: Path,
) -> None:
    """Explicitly selected display punctuation follows; unselected stays."""
    from roughcut.application.content_drafts import create_content_draft_editor_child
    from roughcut.domain.content_draft import SourceExcerptBlock

    project_path, bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        snapshot = draft_editor_module.load_draft_editor_snapshot(
            project_path,
            source_bindings=bindings,
            content_draft_id=draft_id,
        )
        # seed display text that differs from canonical by punctuation only:
        # canonical 「主持人说：‘请大家重点内容然后再讨论下一步安排。’」
        # display    「主持人说：“请大家重点内容然后再讨论下一步安排。”」
        display_text = "主持人说：“请大家重点内容然后再讨论下一步安排。”"
        child = create_content_draft_editor_child(
            project_path,
            parent=snapshot.candidate,
            blocks=tuple(
                replace(block, display_text=display_text)
                if isinstance(block, SourceExcerptBlock)
                and block.block_id == "block_00"
                else block
                for block in snapshot.candidate.blocks
            ),
            expected_revision=ProjectStore(project_path).load().revision,
        )
        display_draft_id = child.content_draft.content_draft_id
        status, _selected = _request(
            review,
            "POST",
            "/api/workflow/draft-candidate-select",
            headers=_headers(review, write=True),
            body=_workspace_body(
                review,
                {
                    "parent_candidate_id": draft_id,
                    "child_candidate_id": display_draft_id,
                },
            ),
        )
        assert status == 200, _selected
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        assert isinstance(editor, dict)
        paragraphs = editor["paragraphs"]
        assert isinstance(paragraphs, list)
        block_00 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        block_01 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_01"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict) and isinstance(block_01, dict)
        # unselected punctuation: select canonical 请大家重点 only (6..11 in
        # both canonical and display) — the quotes stay outside the selection
        status, moved = _m3_4a_schema2_move(
            review,
            editor,
            anchor_para=block_00,
            anchor_utf16=6,
            focus_utf16=11,
            source_block_id="block_00",
            target_para=block_01,
            target_block_id="block_01",
            target_utf16=0,
        )
        assert status == 201, moved
        result_selection = moved["result_selection"]
        assert isinstance(result_selection, dict)
        response = result_selection["response"]
        assert isinstance(response, dict)
        resolution = response["resolution"]
        assert isinstance(resolution, dict)
        assert resolution["canonical_text"] == "请大家重点"
        refs = resolution["refs"]
        assert isinstance(refs, list) and len(refs) == 1
        assert refs[0]["start_ticks"] == 19_200  # type: ignore[index]
        assert refs[0]["end_ticks"] == 35_200  # type: ignore[index]
        # the split fragments keep the unselected punctuation in place
        blocks = moved["draft_editor"]["blocks"]
        assert isinstance(blocks, list)
        source_blocks = [b for b in blocks if isinstance(b, dict) and b.get("kind") == "source_excerpt"]
        left_piece = next(
            b for b in source_blocks if b.get("canonical_text") == "主持人说"
        )
        assert left_piece["display_text"] == "主持人说：“"  # type: ignore[index]
        right_piece = next(
            b for b in source_blocks if b.get("canonical_text") == "内容然后再讨论下一步安排"
        )
        assert right_piece["display_text"] == "内容然后再讨论下一步安排。”"  # type: ignore[index]
    finally:
        review.close()


def test_m3_4_a_narration_object_envelope_closed_set_and_atomic(
    tmp_path: Path,
) -> None:
    """Narration moves via a closed object envelope without result_selection."""
    project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        narration_para = next(
            item for item in paragraphs
            if isinstance(item, dict) and item.get("block_id") == "block_narration"
        )
        assert isinstance(narration_para, dict)
        narration_text = str(narration_para["text"])
        # narration source envelope: closed set, no resolver fields
        source = {
            "kind": "narration_block",
            "surface": "draft",
            "block_id": "block_narration",
            "text": narration_text,
            "status": "draft",
            "recorded_refs": [],
        }
        block_00 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict)
        before_files = sorted((project_path / "content-drafts").glob("*.json"))
        body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": source,
                "target": {
                    "paragraph_id": block_00["paragraph_id"],
                    "block_id": "block_00",
                    "utf16_offset": 0,
                },
            },
        )
        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        assert status == 201, moved
        assert "result_selection" not in moved
        assert "result_highlight" not in moved
        child_id = moved["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert child_id != draft_id
        assert moved["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == 2  # type: ignore[index]
        assert len(sorted((project_path / "content-drafts").glob("*.json"))) == len(
            before_files
        ) + 1
        moved_blocks = moved["draft_editor"]["blocks"]
        assert isinstance(moved_blocks, list)
        moved_narration = next(
            b for b in moved_blocks
            if isinstance(b, dict) and b.get("kind") == "narration"
        )
        assert moved_narration["text"] == narration_text  # type: ignore[index]
        assert moved_narration["status"] == "draft"  # type: ignore[index]

        # --- unknown field -> 400 zero writes ---
        unknown = {**source, "resolution_hash": "x" * 64}
        unknown_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": unknown,
                "target": {
                    "paragraph_id": block_00["paragraph_id"],
                    "block_id": "block_00",
                    "utf16_offset": 0,
                },
            },
        )
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=unknown_body,
        )
        assert status == 400, rejected
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert len(sorted((project_path / "content-drafts").glob("*.json"))) == len(
            before_files
        ) + 1

        # --- text mismatch -> 400 zero writes ---
        mismatch_source = {**source, "text": "改了文字的解说"}
        mismatch_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": mismatch_source,
                "target": {
                    "paragraph_id": block_00["paragraph_id"],
                    "block_id": "block_00",
                    "utf16_offset": 0,
                },
            },
        )
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=mismatch_body,
        )
        assert status == 400, rejected
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert len(sorted((project_path / "content-drafts").glob("*.json"))) == len(
            before_files
        ) + 1

        # --- stale block id -> 400 zero writes ---
        stale_source = {**source, "block_id": "block_does_not_exist"}
        stale_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": stale_source,
                "target": {
                    "paragraph_id": block_00["paragraph_id"],
                    "block_id": "block_00",
                    "utf16_offset": 0,
                },
            },
        )
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=stale_body,
        )
        assert status == 400, rejected
        assert len(sorted((project_path / "content-drafts").glob("*.json"))) == len(
            before_files
        ) + 1

        # --- target inside the narration block itself -> 400 zero writes ---
        inside_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": source,
                "target": {
                    "paragraph_id": narration_para["paragraph_id"],
                    "block_id": "block_narration",
                    "utf16_offset": 1,
                },
            },
        )
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=inside_body,
        )
        assert status == 400, rejected
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert len(sorted((project_path / "content-drafts").glob("*.json"))) == len(
            before_files
        ) + 1
        after = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(after, dict)
        assert after["candidate"]["candidate_id"] == child_id  # type: ignore[index]
        assert after["workspace"] == moved["draft_editor"]["workspace"]  # type: ignore[index]
    finally:
        review.close()


def test_m3_4_a_result_selection_publish_preflight_failure_is_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-publish inconsistency fails closed with zero writes."""
    project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        block_00 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        block_01 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_01"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict) and isinstance(block_01, dict)
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": draft_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 6,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 11,
                    "offset_encoding": "utf16",
                },
            },
        )
        assert status == 200, selection
        assert isinstance(selection, dict)
        before_files = sorted((project_path / "content-drafts").glob("*.json"))
        baseline_revision = ProjectStore(project_path).load().revision
        body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": _schema2_drop_source(initial, selection),
                "target": {
                    "paragraph_id": block_01["paragraph_id"],
                    "block_id": "block_01",
                    "utf16_offset": 9,
                },
            },
        )
        import roughcut.application.draft_workspaces as draft_workspaces_module

        original = draft_workspaces_module.prepare_draft_editor_result_selection

        def poisoned(*args: object, **kwargs: object) -> dict[str, object]:
            raise ProjectError("injected pre-publish result selection failure")

        monkeypatch.setattr(
            draft_workspaces_module,
            "prepare_draft_editor_result_selection",
            poisoned,
        )
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=body,
        )
        monkeypatch.setattr(
            draft_workspaces_module,
            "prepare_draft_editor_result_selection",
            original,
        )
        assert status == 400, rejected
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert "injected pre-publish" in str(rejected["error"]["message"])  # type: ignore[index]
        assert sorted((project_path / "content-drafts").glob("*.json")) == before_files
        assert ProjectStore(project_path).load().revision == baseline_revision
        unchanged = _request(review, "GET", "/api/workflow/draft-editor")[1]
        assert isinstance(unchanged, dict)
        assert unchanged["candidate"]["candidate_id"] == draft_id  # type: ignore[index]
        assert unchanged["workspace"] == initial["workspace"]  # type: ignore[index]
        workspace_file = project_path / "workflow" / "draft-workspaces" / "wfr_p1.json"
        assert json.loads(workspace_file.read_text(encoding="utf-8"))["generation"] == 1
    finally:
        review.close()


def test_m3_4_a_result_selection_hash_supports_immediate_redrag(
    tmp_path: Path,
) -> None:
    """Move then immediately re-drag the moved content via the real server.

    The result_selection resolution_hash is validated by the server against
    the client-round-tripped schema-2 source shape; the hash built by
    prepare_draft_editor_result_selection must match that shape or the
    second move fails with `schema 2 resolved selection hash is stale`.
    This exercises the real `_resolve_schema2_drop_selection` validation
    path end to end (two HTTP move_selection requests).
    """
    project_path, _bindings, draft_id, review = _m3_4a_server(tmp_path)
    try:
        # --- first move: block_00 -> block_01 (same as the real failure) ---
        status, initial = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, initial
        assert isinstance(initial, dict)
        paragraphs = initial["paragraphs"]
        assert isinstance(paragraphs, list)
        block_00 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_00"
                for run in item.get("source_runs", [])
            )
        )
        block_01 = next(
            item for item in paragraphs
            if isinstance(item, dict)
            and any(
                isinstance(run, dict) and run.get("block_id") == "block_01"
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_00, dict) and isinstance(block_01, dict)
        status, selection = _request(
            review,
            "POST",
            "/api/workflow/draft-selection-resolve",
            headers=_headers(review, write=True),
            body={
                "candidate_id": draft_id,
                "surface": "draft",
                "anchor": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 6,
                    "offset_encoding": "utf16",
                },
                "focus": {
                    "paragraph_id": block_00["paragraph_id"],
                    "offset": 11,
                    "offset_encoding": "utf16",
                },
            },
        )
        assert status == 200, selection
        assert isinstance(selection, dict)
        first_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": _schema2_drop_source(initial, selection),
                "target": {
                    "paragraph_id": block_01["paragraph_id"],
                    "block_id": "block_01",
                    "utf16_offset": 9,
                },
            },
        )
        status, moved = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=first_body,
        )
        assert status == 201, moved
        result_selection = moved["result_selection"]
        assert isinstance(result_selection, dict)
        response = result_selection["response"]
        assert isinstance(response, dict)
        first_hash = response["resolution_hash"]
        assert isinstance(first_hash, str) and len(first_hash) == 64

        # --- second move: re-drag the moved selection to another position ---
        # The client rebuilds the schema-2 source from the result_selection:
        # display_range via schema2DisplayPoint, block_ids from the current
        # snapshot source runs, refs/canonical/degraded from the response.
        status, editor = _request(review, "GET", "/api/workflow/draft-editor")
        assert status == 200, editor
        assert isinstance(editor, dict)
        current_paragraphs = editor["paragraphs"]
        assert isinstance(current_paragraphs, list)
        moved_paragraph = next(
            item for item in current_paragraphs
            if isinstance(item, dict)
            and item.get("paragraph_id")
            == result_selection["request"]["anchor"]["paragraph_id"]
        )
        assert isinstance(moved_paragraph, dict)
        runs = moved_paragraph.get("source_runs", [])
        assert isinstance(runs, list) and runs
        anchor_char = response["display_range"]["anchor"]["character_offset"]
        focus_char = response["display_range"]["focus"]["character_offset"]
        assert isinstance(anchor_char, int) and isinstance(focus_char, int)

        def block_at(character_offset: int) -> str:
            for index, run in enumerate(runs):
                if not isinstance(run, dict):
                    continue
                start = run.get("start_offset")
                end = run.get("end_offset")
                block_id = run.get("block_id")
                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or not isinstance(block_id, str)
                ):
                    continue
                if start <= character_offset < end:
                    return block_id
                if character_offset == end:
                    next_run = runs[index + 1] if index + 1 < len(runs) else None
                    next_start = next_run.get("start_offset") if isinstance(next_run, dict) else None
                    if next_start != character_offset:
                        return block_id
            raise AssertionError(f"no block for offset {character_offset}")

        anchor_block = block_at(int(anchor_char))
        focus_block = block_at(int(focus_char))
        block_ids = []
        for run in runs:
            if not isinstance(run, dict):
                continue
            block_id = run.get("block_id")
            start = run.get("start_offset")
            end = run.get("end_offset")
            if (
                not isinstance(block_id, str)
                or not isinstance(start, int)
                or not isinstance(end, int)
            ):
                continue
            if end <= anchor_char or focus_char <= start:
                continue
            if not block_ids or block_ids[-1] != block_id:
                block_ids.append(block_id)
        assert block_ids, "second move source block ids must be non-empty"
        resolution = response["resolution"]
        assert isinstance(resolution, dict)
        refs = resolution["refs"]
        assert isinstance(refs, list) and refs
        # target: another source paragraph from the current snapshot (all
        # block ids are regenerated after the first move)
        block_02 = next(
            item for item in current_paragraphs
            if isinstance(item, dict)
            and item.get("paragraph_id")
            != str(moved_paragraph["paragraph_id"])
            and any(
                isinstance(run, dict) and isinstance(run.get("block_id"), str)
                for run in item.get("source_runs", [])
            )
        )
        assert isinstance(block_02, dict)
        target_run = next(
            run for run in block_02.get("source_runs", [])
            if isinstance(run, dict) and isinstance(run.get("block_id"), str)
        )
        assert isinstance(target_run, dict)
        target_block_id = str(target_run["block_id"])
        second_body = _workspace_body(
            review,
            {
                "schema_version": 2,
                "operation": "move_selection",
                "accept_degraded": False,
                "source": {
                    "kind": "resolved_selection",
                    "resolution_hash": first_hash,
                    "surface": "draft",
                    "selection_kind": "source_excerpt",
                    "display_range": {
                        "anchor": {
                            "paragraph_id": str(moved_paragraph["paragraph_id"]),
                            "block_id": anchor_block,
                            "utf16_offset": int(
                                response["display_range"]["anchor"]["utf16_offset"]
                            ),
                        },
                        "focus": {
                            "paragraph_id": str(moved_paragraph["paragraph_id"]),
                            "block_id": focus_block,
                            "utf16_offset": int(
                                response["display_range"]["focus"]["utf16_offset"]
                            ),
                        },
                    },
                    "block_ids": block_ids,
                    "refs": [
                        {
                            "source_id": ref["source_id"],
                            "transcript_version_id": ref["transcript_version_id"],
                            "segment_id": ref["segment_id"],
                            "start_ticks": ref["start_ticks"],
                            "end_ticks": ref["end_ticks"],
                        }
                        for ref in refs
                        if isinstance(ref, dict)
                    ],
                    "canonical_text": resolution["canonical_text"],
                    "degraded": resolution["degraded"],
                },
                "target": {
                    "paragraph_id": str(block_02["paragraph_id"]),
                    "block_id": target_block_id,
                    "utf16_offset": 0,
                },
            },
        )
        status, redragged = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=second_body,
        )
        assert status == 201, redragged
        assert redragged["draft_editor"]["candidate"]["candidate_id"] != moved["draft_editor"]["candidate"]["candidate_id"]  # type: ignore[index]
        assert redragged["draft_editor"]["workspace"]["expected_checkpoint_ref"]["generation"] == 3  # type: ignore[index]

        # --- negative: tampering with the returned refs must still fail ---
        # replay the identical envelope with a fresh operation id for the
        # current generation (3) so the tamper reaches the hash check
        tampered = dict(second_body)
        tampered_source = dict(tampered["source"])  # type: ignore[index]
        tampered_refs = [dict(ref) for ref in tampered_source["refs"]]  # type: ignore[index]
        tampered_refs[0]["start_ticks"] = int(tampered_refs[0]["start_ticks"]) + 1
        tampered_source["refs"] = tampered_refs
        tampered["source"] = tampered_source  # type: ignore[index]
        tampered["operation_id"] = f"dwop_3_{secrets.token_hex(16)}"
        tampered["expected_checkpoint_ref"] = redragged["draft_editor"]["workspace"]["expected_checkpoint_ref"]  # type: ignore[index]
        tampered["expected_current_candidate_ref"] = redragged["draft_editor"]["workspace"]["expected_current_candidate_ref"]  # type: ignore[index]
        before_files = sorted((project_path / "content-drafts").glob("*.json"))
        status, rejected = _request(
            review,
            "POST",
            "/api/workflow/draft-edit",
            headers=_headers(review, write=True),
            body=tampered,
        )
        assert status == 400, rejected
        assert rejected["error"]["code"] == "invalid_workflow_change"  # type: ignore[index]
        assert sorted((project_path / "content-drafts").glob("*.json")) == before_files
    finally:
        review.close()
