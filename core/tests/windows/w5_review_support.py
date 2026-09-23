"""Bounded synthetic workflow fixtures for W5 Review tests and browser smoke."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Literal

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.preview import load_review_snapshot
from roughcut.application.projects import create_project
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflow_review import load_workflow_review_snapshot
from roughcut.application.workflows import workflow_action, workflow_start, workflow_status
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance, TranscriptSegment

ReviewSurface = Literal["scope_review", "draft_review", "roughcut_review", "export_review"]
RUN_ID = "wfr_w5"
SOURCE_ID = "src_w5"
TRANSCRIPT_ID = "tr_w5"


def _source(media_path: Path) -> SourceAsset:
    return SourceAsset(
        source_id=SOURCE_ID,
        kind="video",
        display_name="素材 Windows 中文 with spaces.mp4",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(media_path.resolve())},
        fingerprint=fingerprint_file(media_path),
        probe=MediaProbe(
            duration_ticks=240_000,
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
    )


def _transcript() -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=TRANSCRIPT_ID,
        source_id=SOURCE_ID,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="1",
            models={},
            parameters={},
            raw_result_path="raw-asr/src_w5/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_w5",
                start_ticks=0,
                end_ticks=240_000,
                original_text="Windows 中文审阅。",
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            ),
        ),
    )


def _seed_project(root: Path) -> None:
    project = create_project(root, "Windows 中文 Review")
    media_path = root / "素材 Windows 中文 with spaces.mp4"
    fixture_media = Path(__file__).with_name("fixtures") / "w5-review.mp4"
    media_path.write_bytes(fixture_media.read_bytes())
    source = _source(media_path)
    transcript = _transcript()
    write_new_json(
        root / "transcripts" / SOURCE_ID / f"{TRANSCRIPT_ID}.json",
        transcript.to_dict(),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=1,
            sources=(source,),
            active_transcript_versions={SOURCE_ID: TRANSCRIPT_ID},
        ),
        expected_revision=0,
    )


def _approve_scope(root: Path) -> None:
    started = workflow_start(root, RUN_ID, [SOURCE_ID])
    scope_basis = started.status["confirmation_bases"]["scope"]["basis"]
    workflow_action(
        root,
        RUN_ID,
        "act_w5_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": scope_basis,
            "source_authorizations": [
                {
                    "source_id": SOURCE_ID,
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )


def _approve_outline(root: Path) -> None:
    scoped = workflow_status(root, RUN_ID)
    brief_basis = scoped["confirmation_bases"]["brief"]["basis"]
    workflow_action(
        root,
        RUN_ID,
        "act_w5_brief",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": brief_basis,
            "theme": "Windows Review",
            "target_duration_ticks": 240_000,
            "focus": ["中文审阅"],
            "allow_reorder": True,
            "speaker_resolution_waivers": [],
        },
    )
    outline = workflow_action(
        root,
        RUN_ID,
        "act_w5_outline",
        "submit_outline",
        {
            "schema_version": 1,
            "title": "Windows Review",
            "opening": "开场",
            "sections": [
                {
                    "section_id": f"section_w5_{index}",
                    "title": title,
                    "summary": title,
                    "target_duration_ticks": 60_000,
                }
                for index, title in enumerate(("开场", "主体", "重点", "结尾"), start=1)
            ],
            "ending": "结尾",
            "required_content_coverage": [],
            "narration_status": "none",
        },
    )
    outline_ref = outline.status["presented_subjects"]["outline_ref"]
    workflow_action(
        root,
        RUN_ID,
        "act_w5_outline_approve",
        "approve_outline",
        {"schema_version": 1, "outline_ref": outline_ref},
    )


def _submit_draft(root: Path, *, include_narration: bool = False) -> None:
    bindings = [{"source_id": SOURCE_ID, "transcript_version_id": TRANSCRIPT_ID}]
    status = workflow_status(root, RUN_ID)
    brief_ref = status["workflow_run"]["artifact_refs"]["brief"]
    snapshot = load_workflow_review_snapshot(root, source_bindings=bindings)
    if snapshot.context_hash is None:
        raise AssertionError("W5 fixture did not produce a workflow context hash")
    blocks: list[dict[str, object]] = [
        {
            "block_id": "block_w5",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": SOURCE_ID,
                    "transcript_version_id": TRANSCRIPT_ID,
                    "segment_id": "seg_w5",
                    "start_ticks": 0,
                    "end_ticks": 240_000,
                }
            ],
            "canonical_text": "Windows 中文审阅。",
        }
    ]
    if include_narration:
        blocks.append(
            {
                "block_id": "narration_w5",
                "kind": "narration",
                "text": "待录音解说。",
                "status": "draft",
                "recorded_refs": [],
            }
        )
    workflow_action(
        root,
        RUN_ID,
        "act_w5_draft",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "Windows Review Draft",
            "source_bindings": bindings,
            "brief_ref": brief_ref,
            "context_hash": snapshot.context_hash,
            "blocks": blocks,
            "scoped_mutable_block_ids": [],
        },
    )


def _approve_draft(root: Path) -> None:
    status = workflow_status(root, RUN_ID)
    draft_ref = status["workflow_run"]["artifact_refs"]["content_draft"]
    workflow_action(
        root,
        RUN_ID,
        "act_w5_approve_draft",
        "approve_draft",
        {"schema_version": 1, "content_draft_ref": draft_ref},
    )


def _adopt_roughcut(root: Path) -> None:
    status = workflow_status(root, RUN_ID)
    proposal_ref = status["workflow_run"]["artifact_refs"]["proposal"]
    workflow_action(
        root,
        RUN_ID,
        "act_w5_adopt_roughcut",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal_ref},
    )


def seed_review_project(
    root: Path,
    surface: ReviewSurface,
    *,
    include_narration: bool = False,
) -> Path:
    """Create a disposable project at one exact workflow Review surface."""
    if root.exists():
        raise ValueError(f"fixture root already exists: {root}")
    _seed_project(root)
    _approve_scope(root)
    if surface == "scope_review":
        return root
    _approve_outline(root)
    _submit_draft(root, include_narration=include_narration)
    if surface == "draft_review":
        return root
    if include_narration:
        raise ValueError("narration fixtures are only valid for draft_review")
    _approve_draft(root)
    if surface == "roughcut_review":
        return root
    _adopt_roughcut(root)
    return root


def seed_artifact_review_project(root: Path) -> tuple[Path, str, Path]:
    """Create a roughcut Review fixture and return its exact proposal identity."""
    seed_review_project(root, "roughcut_review")
    status = workflow_status(root, RUN_ID)
    proposal_ref = status["workflow_run"]["artifact_refs"]["proposal"]
    if not isinstance(proposal_ref, dict) or not isinstance(proposal_ref.get("artifact_id"), str):
        raise TypeError("W5 fixture did not produce a Proposal ref")
    source = ProjectStore(root).load().sources[0]
    media_path = Path(source.locator["absolute_path"])
    # Force the same production Proposal/Review loader used by the server before
    # handing the fixture to a child process.
    load_review_snapshot(root, proposal_id=proposal_ref["artifact_id"])
    return root, proposal_ref["artifact_id"], media_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--surface",
        choices=("scope_review", "draft_review", "roughcut_review", "export_review"),
        required=True,
    )
    args = parser.parse_args()
    seed_review_project(Path(args.project), args.surface)
    print(json.dumps({"run_id": RUN_ID, "surface": args.surface}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
