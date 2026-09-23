from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.application.proposals as proposal_module
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import create_edit_brief, read_multi_source_agent_context
from roughcut.application.projects import create_project
from roughcut.application.proposals import (
    confirm_multi_source_edit_proposal,
    create_multi_source_edit_proposal,
    read_decision,
    read_edit_decision,
    read_multi_source_edit_decision,
)
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.timeline import VirtualTimeline
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance, TranscriptSegment
from roughcut.mcp import handle_request


def _source(source_id: str, index: int) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name=f"素材 {source_id}.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": f"/private/绝对路径/{source_id}.wav"},
        fingerprint=SourceFingerprint(100 + index, index, f"fingerprint-{index}"),
        probe=MediaProbe(
            duration_ticks=600_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec=None,
            width=None,
            height=None,
            nominal_frame_rate=None,
            is_vfr=False,
            audio_codec="pcm_s16le",
            audio_sample_rate=16_000,
            rotation_degrees=0,
        ),
        tags=("访谈", source_id),
        note=f"{source_id} 备注",
    )


def _transcript(source_id: str, transcript_id: str, texts: tuple[str, str]) -> TimedTranscript:
    segment_ids = ("seg_shared", f"seg_{source_id}_2")
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path=f"raw-asr/{source_id}/private.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=tuple(
            TranscriptSegment(
                segment_id=segment_id,
                start_ticks=index * 120_000,
                end_ticks=(index + 1) * 120_000,
                original_text=text,
                corrected_text=None,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            )
            for index, (segment_id, text) in enumerate(zip(segment_ids, texts, strict=True))
        ),
    )


def _setup_project(tmp_path: Path, *, allow_reorder: bool = True) -> dict[str, object]:
    project_path = tmp_path / "跨素材 project"
    project = create_project(project_path, "跨素材")
    source_a = _source("src_a", 0)
    source_b = _source("src_b", 1)
    transcript_a = _transcript("src_a", "tr_a", ("A 开场。", "A 回归。"))
    transcript_b = _transcript("src_b", "tr_b", ("B 中段。", "B 补充。"))
    for transcript in (transcript_a, transcript_b):
        path = (
            project_path
            / "transcripts"
            / transcript.source_id
            / f"{transcript.transcript_version_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8")
    person_a = Person("person_a", "人物 A", "guest", "")
    person_b = Person("person_b", "人物 B", "guest", "")
    imported = replace(
        project,
        revision=1,
        sources=(source_a, source_b),
        persons=(person_a, person_b),
        speaker_maps=(
            SpeakerMap("src_a", "tr_a", "spk_0", "person_a", True),
            SpeakerMap("src_b", "tr_b", "spk_0", "person_b", True),
        ),
        active_transcript_versions={"src_a": "tr_a", "src_b": "tr_b"},
    )
    ProjectStore(project_path).save(imported, expected_revision=0)
    brief = create_edit_brief(
        project_path,
        theme="跨素材主题",
        target_duration_ticks=360_000,
        focus=["A", "B"],
        allow_reorder=allow_reorder,
        expected_revision=1,
    )
    bindings = [
        {"source_id": "src_a", "transcript_version_id": "tr_a"},
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
    ]
    context = read_multi_source_agent_context(
        project_path,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=10,
    )
    return {
        "project_path": project_path,
        "revision": brief.project_revision,
        "brief_id": brief.brief.brief_id,
        "bindings": bindings,
        "context_hash": context.context_hash,
    }


def _clips() -> list[dict[str, object]]:
    return [
        {
            "clip_id": "clip_a_open",
            "source_id": "src_a",
            "transcript_version_id": "tr_a",
            "segment_id": "seg_shared",
            "source_in_ticks": 0,
            "source_out_ticks": 120_000,
            "reason": "A 开场",
            "display_text": "A 开场。",
        },
        {
            "clip_id": "clip_b_middle",
            "source_id": "src_b",
            "transcript_version_id": "tr_b",
            "segment_id": "seg_shared",
            "source_in_ticks": 0,
            "source_out_ticks": 120_000,
            "reason": "B 中段",
            "display_text": "B 中段。",
        },
        {
            "clip_id": "clip_a_return",
            "source_id": "src_a",
            "transcript_version_id": "tr_a",
            "segment_id": "seg_src_a_2",
            "source_in_ticks": 120_000,
            "source_out_ticks": 240_000,
            "reason": "A 回归",
            "display_text": "A 回归。",
        },
    ]


def _create(state: dict[str, object], **overrides: object):
    arguments: dict[str, object] = {
        "source_bindings": state["bindings"],
        "brief_id": state["brief_id"],
        "context_hash": state["context_hash"],
        "clips": _clips(),
        "total_duration_ticks": 360_000,
        "expected_revision": state["revision"],
        **overrides,
    }
    return create_multi_source_edit_proposal(
        Path(state["project_path"]),
        **arguments,  # type: ignore[arg-type]
    )


def test_multi_source_context_is_ordered_paged_private_and_resolves_people(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    first = read_multi_source_agent_context(
        Path(state["project_path"]),
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        brief_id=str(state["brief_id"]),
        expected_revision=int(state["revision"]),
        offset=0,
        limit=2,
    )
    second = read_multi_source_agent_context(
        Path(state["project_path"]),
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        brief_id=str(state["brief_id"]),
        expected_revision=int(state["revision"]),
        offset=2,
        limit=2,
    )

    assert first.schema_version == 2
    assert first.context_hash == second.context_hash == state["context_hash"]
    assert [source["source_id"] for source in first.sources] == ["src_a", "src_b"]
    assert [(segment["source_id"], segment["segment_id"]) for segment in first.segments] == [
        ("src_a", "seg_shared"),
        ("src_a", "seg_src_a_2"),
    ]
    assert [(segment["source_id"], segment["segment_id"]) for segment in second.segments] == [
        ("src_b", "seg_shared"),
        ("src_b", "seg_src_b_2"),
    ]
    assert first.segments[0]["transcript_version_id"] == "tr_a"
    assert first.segments[0]["local_speaker_id"] == "spk_0"
    assert first.segments[0]["person_id"] == "person_a"
    assert first.segments[0]["person_name"] == "人物 A"
    assert second.segments[0]["person_id"] == "person_b"
    reversed_context = read_multi_source_agent_context(
        Path(state["project_path"]),
        source_bindings=list(reversed(state["bindings"])),  # type: ignore[arg-type]
        brief_id=str(state["brief_id"]),
        expected_revision=int(state["revision"]),
        offset=0,
        limit=2,
    )
    assert reversed_context.context_hash != first.context_hash
    assert [source["source_id"] for source in reversed_context.sources] == ["src_b", "src_a"]
    assert [segment["source_id"] for segment in reversed_context.segments] == [
        "src_b",
        "src_b",
    ]
    payload = json.dumps(first.to_dict(), ensure_ascii=False)
    assert "/private/" not in payload
    assert "locator" not in payload
    assert "fingerprint" not in payload
    assert "raw-asr" not in payload


def test_allow_reorder_true_creates_and_confirms_a_b_a_decision(tmp_path: Path) -> None:
    state = _setup_project(tmp_path, allow_reorder=True)
    before = ProjectStore(Path(state["project_path"])).load()
    proposal = _create(state)
    assert proposal.proposal.schema_version == 2
    assert [binding.source_id for binding in proposal.proposal.source_bindings] == [
        "src_a",
        "src_b",
    ]
    assert ProjectStore(Path(state["project_path"])).load() == before

    decision = confirm_multi_source_edit_proposal(
        Path(state["project_path"]),
        proposal.proposal.proposal_id,
        expected_revision=int(state["revision"]),
    )
    project = ProjectStore(Path(state["project_path"])).load()
    assert decision.decision.schema_version == 2
    assert project.revision == int(state["revision"]) + 1
    assert project.active_edit_version_id == decision.decision.edit_version_id
    assert (
        read_multi_source_edit_decision(
            Path(state["project_path"]), decision.decision.edit_version_id
        )
        == decision
    )
    assert [span.source_id for span in VirtualTimeline.from_decision(decision.decision).spans] == [
        "src_a",
        "src_b",
        "src_a",
    ]

    with pytest.raises(ProjectError):
        confirm_multi_source_edit_proposal(
            Path(state["project_path"]),
            proposal.proposal.proposal_id,
            expected_revision=project.revision,
        )
    assert ProjectStore(Path(state["project_path"])).load() == project


def test_allow_reorder_false_rejects_a_b_a(tmp_path: Path) -> None:
    state = _setup_project(tmp_path, allow_reorder=False)
    with pytest.raises(ProjectError, match="reorder"):
        _create(state)
    assert not (Path(state["project_path"]) / "proposals").exists()


@pytest.mark.parametrize(
    "replacement",
    [
        {"source_id": "src_unknown"},
        {"transcript_version_id": "tr_b"},
        {"segment_id": "seg_src_b_2"},
        {"segment_id": "seg_unknown"},
        {"source_out_ticks": 600_001},
        {"source_out_ticks": 0},
        {"display_text": "B 中段。"},
        {"display_text": "A 开场"},
        {
            "source_in_ticks": 10_000,
            "source_out_ticks": 110_000,
            "display_text": "开场",
        },
    ],
)
def test_multi_source_proposal_rejects_cross_source_misuse_and_bad_ranges(
    tmp_path: Path, replacement: dict[str, object]
) -> None:
    state = _setup_project(tmp_path)
    clips = _clips()
    clips[0] = {**clips[0], **replacement}
    with pytest.raises(ProjectError):
        _create(state, clips=clips)
    assert not (Path(state["project_path"]) / "proposals").exists()


def test_multi_source_proposal_rejects_stale_revision_context_binding_and_total(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    invalid_bindings = [
        {"source_id": "src_a", "transcript_version_id": "tr_wrong"},
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
    ]
    duplicate_clips = _clips()
    duplicate_clips[1] = {**duplicate_clips[1], "clip_id": "clip_a_open"}
    for overrides in (
        {"expected_revision": int(state["revision"]) - 1},
        {"context_hash": "0" * 64},
        {"source_bindings": invalid_bindings},
        {"brief_id": "brief_unknown"},
        {"total_duration_ticks": 359_999},
        {"clips": duplicate_clips},
    ):
        with pytest.raises(ProjectError):
            _create(state, **overrides)
    assert not (Path(state["project_path"]) / "proposals").exists()


def test_multi_source_proposal_and_decision_save_failures_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _setup_project(tmp_path / "proposal-failure")
    project_path = Path(state["project_path"])
    before = ProjectStore(project_path).load()

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("proposal write failed")

    monkeypatch.setattr(proposal_module, "write_new_json", fail_write)
    with pytest.raises(OSError, match="proposal write failed"):
        _create(state)
    assert ProjectStore(project_path).load() == before
    assert not (project_path / "proposals").exists()

    monkeypatch.undo()
    decision_state = _setup_project(tmp_path / "decision-failure")
    decision_path = Path(decision_state["project_path"])
    proposal = _create(decision_state)
    before_decision = ProjectStore(decision_path).load()
    original_save = ProjectStore.save

    def fail_decision_save(self, project, *, expected_revision):  # type: ignore[no-untyped-def]
        if project.active_edit_version_id is not None:
            raise OSError("decision save failed")
        return original_save(self, project, expected_revision=expected_revision)

    monkeypatch.setattr(ProjectStore, "save", fail_decision_save)
    with pytest.raises(OSError, match="decision save failed"):
        confirm_multi_source_edit_proposal(
            decision_path,
            proposal.proposal.proposal_id,
            expected_revision=int(decision_state["revision"]),
        )
    assert ProjectStore(decision_path).load() == before_decision
    assert list((decision_path / "edits").glob("*.json")) == []


def test_cli_and_mcp_return_the_same_multi_source_context(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "multi-source-context",
            "--project",
            str(state["project_path"]),
            "--source-bindings-json",
            json.dumps(state["bindings"]),
            "--brief-id",
            str(state["brief_id"]),
            "--expected-revision",
            str(state["revision"]),
            "--offset",
            "0",
            "--limit",
            "3",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    cli_payload = json.loads(cli.stdout)

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "multi_source_context",
                "arguments": {
                    "project_path": str(state["project_path"]),
                    "source_bindings": state["bindings"],
                    "brief_id": state["brief_id"],
                    "expected_revision": state["revision"],
                    "offset": 0,
                    "limit": 3,
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"] == cli_payload


def test_public_multi_source_create_and_confirm_require_workflow_but_domain_readback_stays_available(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    project_path = Path(state["project_path"])
    before = ProjectStore(project_path).load()
    created = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "multi-source-proposal-create",
            "--project",
            str(state["project_path"]),
            "--source-bindings-json",
            json.dumps(state["bindings"]),
            "--brief-id",
            str(state["brief_id"]),
            "--context-hash",
            str(state["context_hash"]),
            "--clips-json",
            json.dumps(_clips(), ensure_ascii=False),
            "--total-duration-ticks",
            "360000",
            "--expected-revision",
            str(state["revision"]),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 2
    assert json.loads(created.stdout)["error"]["code"] == "workflow_required"
    assert ProjectStore(project_path).load() == before
    confirmed = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "multi_source_proposal_confirm",
                "arguments": {
                    "project_path": str(state["project_path"]),
                    "proposal_id": "proposal_missing",
                    "expected_revision": state["revision"],
                },
            },
        }
    )
    assert confirmed is not None
    confirmed_result = confirmed["result"]
    assert isinstance(confirmed_result, dict)
    decision_payload = confirmed_result["structuredContent"]
    assert isinstance(decision_payload, dict)
    assert decision_payload["error"]["code"] == "workflow_required"
    assert ProjectStore(project_path).load() == before

    proposal = _create(state).proposal
    decision_state = confirm_multi_source_edit_proposal(
        project_path,
        proposal.proposal_id,
        expected_revision=int(state["revision"]),
    )
    decision = decision_state.decision.to_dict()

    readback = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "multi-source-edit-decision-read",
            "--project",
            str(state["project_path"]),
            "--edit-version-id",
            decision_state.decision.edit_version_id,
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert readback.returncode == 0, readback.stderr
    readback_payload = json.loads(readback.stdout)
    assert readback_payload["decision"] == decision
    assert readback_payload["project_revision"] == decision_state.project_revision

    unified = read_decision(
        Path(state["project_path"]), decision_state.decision.edit_version_id
    ).to_dict()
    assert unified == read_decision(
        Path(state["project_path"]), decision_state.decision.edit_version_id
    ).to_dict()
    assert unified["decision"]["type"] == "multi_source_edit_decision"  # type: ignore[index]
    assert unified["decision"]["schema_version"] == 2  # type: ignore[index]
    assert unified["decision"]["payload"] == decision  # type: ignore[index]
    with pytest.raises(ProjectError, match="does not match legacy reader"):
        read_edit_decision(Path(state["project_path"]), decision_state.decision.edit_version_id)
