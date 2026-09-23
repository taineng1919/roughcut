from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path

import pytest

from roughcut.adapters.funasr.runner import FunASRRun
from roughcut.application.agent_context import create_edit_brief, read_agent_context
from roughcut.application.preview import load_review_snapshot
from roughcut.application.projects import create_project, open_project
from roughcut.application.proposals import confirm_edit_proposal, create_edit_proposal
from roughcut.application.renders import render_roughcut
from roughcut.application.sources import add_source, fingerprint_file
from roughcut.application.transcription import transcribe_source
from roughcut.domain.project import ImportMode

ROOT = Path(__file__).resolve().parents[3]
ASR_FIXTURE = ROOT / "fixtures" / "asr" / "funasr_sentence_info.json"
TICKS_PER_SECOND = 120_000

def _make_audio_source(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * 16_000 * 3)


def _fixture_runner(_source_path: Path, raw_output_path: Path) -> FunASRRun:
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASR_FIXTURE, raw_output_path)
    return FunASRRun(
        package_version="1.3.8",
        models={"asr": "fixture-asr", "vad": "fixture-vad", "punc": "fixture-punc"},
        parameters={"device": "cpu", "sentence_timestamp": True},
        started_at="2026-07-19T00:00:00+00:00",
        completed_at="2026-07-19T00:00:01+00:00",
        exit_status=0,
    )


def test_agent_core_flow_reaches_a_reviewable_decision_and_verified_render(
    tmp_path: Path,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    del synthetic_media_runtime
    source_path = tmp_path / "agent-flow.wav"
    _make_audio_source(source_path)
    source_fingerprint = fingerprint_file(source_path)

    project_path = tmp_path / "agent-flow-project"
    project = create_project(project_path, "Agent core flow")
    imported = add_source(
        project_path,
        source_path,
        ImportMode.LINKED,
        expected_revision=project.revision,
    )
    source_id = imported.sources[0].source_id

    transcript = transcribe_source(
        project_path,
        source_id,
        expected_revision=imported.revision,
        runner=_fixture_runner,
    )
    brief = create_edit_brief(
        project_path,
        theme="先结论、后依据",
        target_duration_ticks=204_000,
        focus=["两句 fixture transcript"],
        allow_reorder=True,
        expected_revision=imported.revision + 1,
    )

    first_page = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript.transcript_version_id,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=1,
    )
    second_page = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript.transcript_version_id,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=first_page.next_offset or 0,
        limit=1,
    )
    assert first_page.context_hash == second_page.context_hash
    assert first_page.next_offset == 1
    assert second_page.next_offset is None

    segments = [*first_page.segments, *second_page.segments]
    clips = [
        {
            "clip_id": f"clip_{position}",
            "source_id": source_id,
            "transcript_version_id": transcript.transcript_version_id,
            "segment_id": segment["segment_id"],
            "source_in_ticks": segment["start_ticks"],
            "source_out_ticks": segment["end_ticks"],
            "reason": "fixture conclusion first" if position == 1 else "fixture evidence second",
            "display_text": segment["text"],
        }
        for position, segment in enumerate(reversed(segments), start=1)
    ]
    total_duration_ticks = sum(
        int(clip["source_out_ticks"]) - int(clip["source_in_ticks"])
        for clip in clips
    )
    proposal = create_edit_proposal(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript.transcript_version_id,
        brief_id=brief.brief.brief_id,
        context_hash=first_page.context_hash,
        clips=clips,
        total_duration_ticks=total_duration_ticks,
        expected_revision=brief.project_revision,
    )
    assert proposal.project_revision == brief.project_revision

    decision = confirm_edit_proposal(
        project_path,
        proposal.proposal.proposal_id,
        expected_revision=proposal.project_revision,
    )
    assert decision.project_revision == proposal.project_revision + 1

    review = load_review_snapshot(
        project_path,
        edit_version_id=decision.decision.edit_version_id,
    )
    assert review.basis_type == "decision"
    assert review.requires_new_proposal is True
    assert [span.clip_id for span in review.timeline.spans] == ["clip_1", "clip_2"]
    assert review.timeline.total_duration_ticks == total_duration_ticks
    assert review.timeline.map_output_time(0).source_ticks == 144_000

    rendered = render_roughcut(
        project_path,
        edit_version_id=decision.decision.edit_version_id,
        expected_revision=decision.project_revision,
    )
    assert all(rendered.acceptance.values())
    assert (project_path / rendered.mp4_path).is_file()
    manifest = json.loads(
        (project_path / rendered.manifest_path).read_text(encoding="utf-8")
    )
    assert manifest["acceptance"]["accepted"] is True
    assert [clip["clip_id"] for clip in manifest["clips"]] == ["clip_1", "clip_2"]
    assert manifest["total_duration_ticks"] == total_duration_ticks
    assert total_duration_ticks == pytest.approx(1.7 * TICKS_PER_SECOND)
    assert open_project(project_path).active_edit_version_id == decision.decision.edit_version_id
    assert fingerprint_file(source_path) == source_fingerprint
