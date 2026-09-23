from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.application.transcripts as transcripts_module
import roughcut.cli
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.projects import create_project
from roughcut.application.transcripts import (
    activate_transcript_version,
    correct_transcript,
    read_transcript_versions,
)
from roughcut.application.workflows import workflow_action, workflow_start, workflow_status
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.mcp import handle_request


def _source(source_id: str) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name=f"{source_id}.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": f"/private/fixture/{source_id}.wav"},
        fingerprint=SourceFingerprint(100, 1, f"fingerprint-{source_id}"),
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
    )


def _transcript(
    source_id: str,
    transcript_id: str,
    *,
    parent_id: str | None = None,
    first_correction: str | None = None,
) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=parent_id,
        provenance=TranscriptProvenance(
            backend="funasr",
            package_version="1.3.8",
            models={"asr": "fixture"},
            parameters={"sentence_timestamp": True},
            raw_result_path=f"raw-asr/{source_id}/run_fixture.json",
            started_at="2026-07-01T00:00:00+00:00",
            completed_at="2026-07-01T00:00:01+00:00",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_1",
                start_ticks=0,
                end_ticks=120_000,
                original_text="原始第一句",
                corrected_text=first_correction,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=(
                    FineUnit("token", "原始", 0, 60_000, 0.9),
                    FineUnit("token", "第一句", 60_000, 120_000, 0.92),
                ),
                editorial_mark="include",
            ),
            TranscriptSegment(
                segment_id="seg_2",
                start_ticks=120_000,
                end_ticks=240_000,
                original_text="原始第二句",
                corrected_text=None,
                local_speaker_id="spk_1",
                person_id=None,
                confidence=0.91,
                fine_units=(),
                editorial_mark="maybe",
            ),
        ),
    )


def _write_transcript(project_path: Path, transcript: TimedTranscript) -> Path:
    path = (
        project_path
        / "transcripts"
        / transcript.source_id
        / f"{transcript.transcript_version_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _project(tmp_path: Path) -> tuple[Path, int]:
    project_path = tmp_path / "transcript versions"
    project = create_project(project_path, "Transcript versions")
    root_a = _transcript("src_a", "tr_a_root")
    root_b = _transcript("src_b", "tr_b_root")
    _write_transcript(project_path, root_a)
    _write_transcript(project_path, root_b)
    person_a = Person("person_a", "甲", "guest", "")
    person_b = Person("person_b", "乙", "guest", "")
    updated = replace(
        project,
        revision=1,
        sources=(_source("src_a"), _source("src_b")),
        active_transcript_versions={"src_a": "tr_a_root", "src_b": "tr_b_root"},
        persons=(person_a, person_b),
        speaker_maps=(
            SpeakerMap("src_a", "tr_a_root", "spk_0", "person_a", True),
            SpeakerMap("src_a", "tr_a_root", "spk_1", "person_b", True),
            SpeakerMap("src_b", "tr_b_root", "spk_0", "person_b", True),
        ),
    )
    ProjectStore(project_path).save(updated, expected_revision=0)
    return project_path, updated.revision


def _workflow_hashable_transcript(transcript: TimedTranscript) -> TimedTranscript:
    return replace(
        transcript,
        segments=tuple(
            replace(
                segment,
                confidence=None,
                fine_units=tuple(
                    replace(unit, confidence=None) for unit in segment.fine_units
                ),
            )
            for segment in transcript.segments
        ),
    )


def _start_scope_run(project_path: Path, source_ids: list[str]) -> str:
    project = ProjectStore(project_path).load()
    for source_id, transcript_id in project.active_transcript_versions.items():
        transcript_path = project_path / "transcripts" / source_id / f"{transcript_id}.json"
        transcript = TimedTranscript.from_dict(
            json.loads(transcript_path.read_text(encoding="utf-8"))
        )
        _write_transcript(project_path, _workflow_hashable_transcript(transcript))
    run_id = "wfr_transcript_sync"
    started = workflow_start(project_path, run_id, source_ids)
    workflow_action(
        project_path,
        run_id,
        "act_scope_transcript_sync",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": started.status["confirmation_bases"]["scope"][
                "basis"
            ],
            "source_authorizations": [
                {
                    "source_id": source_id,
                    "transcribe": False,
                    "speaker_diarization": False,
                }
                for source_id in source_ids
            ],
        },
    )
    return run_id


def _binding(run_id: str, project_path: Path, source_id: str):
    run = WorkflowStore(project_path).read_run(run_id)
    return next(item for item in run.ordered_bindings if item.source_id == source_id)


def _brief() -> EditBrief:
    return EditBrief("brief_fixture", "主题", 120_000, ("重点",), True)


def _clip(source_id: str, transcript_id: str, clip_id: str) -> EditClip:
    return EditClip(
        clip_id=clip_id,
        source_id=source_id,
        transcript_version_id=transcript_id,
        segment_id="seg_1",
        source_in_ticks=0,
        source_out_ticks=120_000,
        reason="fixture",
        display_text="fixture",
    )


def _install_schema_one_decision(project_path: Path) -> None:
    project = ProjectStore(project_path).load()
    proposal = EditProposal(
        proposal_id="proposal_one",
        base_project_revision=project.revision,
        base_edit_version_id=None,
        source_id="src_a",
        transcript_version_id="tr_a_root",
        brief_snapshot=_brief(),
        context_hash="a" * 64,
        clips=(_clip("src_a", "tr_a_root", "clip_a"),),
        total_duration_ticks=120_000,
        created_at="fixture",
    )
    decision = EditDecision("edit_one", proposal, project.revision + 1, "fixture")
    edit_path = project_path / "edits" / "edit_one.json"
    edit_path.parent.mkdir(exist_ok=True)
    edit_path.write_text(json.dumps(decision.to_dict()), encoding="utf-8")
    ProjectStore(project_path).save(
        replace(
            project,
            revision=project.revision + 1,
            active_edit_version_id="edit_one",
        ),
        expected_revision=project.revision,
    )


def _install_schema_two_decision(project_path: Path) -> None:
    project = ProjectStore(project_path).load()
    proposal = MultiSourceEditProposal(
        proposal_id="proposal_two",
        base_project_revision=project.revision,
        base_edit_version_id=None,
        source_bindings=(
            SourceTranscriptBinding("src_a", "tr_a_root"),
            SourceTranscriptBinding("src_b", "tr_b_root"),
        ),
        brief_snapshot=_brief(),
        context_hash="b" * 64,
        clips=(
            _clip("src_a", "tr_a_root", "clip_a"),
            _clip("src_b", "tr_b_root", "clip_b"),
        ),
        total_duration_ticks=240_000,
        created_at="fixture",
    )
    decision = MultiSourceEditDecision("edit_two", proposal, project.revision + 1, "fixture")
    edit_path = project_path / "edits" / "edit_two.json"
    edit_path.parent.mkdir(exist_ok=True)
    edit_path.write_text(json.dumps(decision.to_dict()), encoding="utf-8")
    ProjectStore(project_path).save(
        replace(
            project,
            revision=project.revision + 1,
            active_edit_version_id="edit_two",
        ),
        expected_revision=project.revision,
    )


def test_correction_creates_immutable_child_and_inherits_exact_speaker_maps(
    tmp_path: Path,
) -> None:
    project_path, revision = _project(tmp_path)
    parent_path = project_path / "transcripts/src_a/tr_a_root.json"
    parent_bytes = parent_path.read_bytes()
    parent = TimedTranscript.from_dict(json.loads(parent_bytes))

    result = correct_transcript(
        project_path,
        source_id="src_a",
        parent_transcript_version_id="tr_a_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": "  校正第一句  "}],
        expected_revision=revision,
    )

    assert result.changed is True
    assert result.project_revision == revision + 1
    assert result.transcript is not None
    child = result.transcript
    assert child.parent_version_id == "tr_a_root"
    assert child.segments[0].corrected_text == "校正第一句"
    assert child.provenance == parent.provenance
    for before, after in zip(parent.segments, child.segments, strict=True):
        before_data = before.to_dict()
        after_data = after.to_dict()
        assert {key: value for key, value in before_data.items() if key != "corrected_text"} == {
            key: value for key, value in after_data.items() if key != "corrected_text"
        }
    assert parent_path.read_bytes() == parent_bytes

    project = ProjectStore(project_path).load()
    assert project.active_transcript_versions["src_a"] == child.transcript_version_id
    assert [mapping.identity_key for mapping in project.speaker_maps] == [
        ("src_a", "tr_a_root", "spk_0"),
        ("src_a", "tr_a_root", "spk_1"),
        ("src_b", "tr_b_root", "spk_0"),
        ("src_a", child.transcript_version_id, "spk_0"),
        ("src_a", child.transcript_version_id, "spk_1"),
    ]
    assert [mapping.person_id for mapping in project.speaker_maps[-2:]] == [
        "person_a",
        "person_b",
    ]


def test_same_correction_is_noop_and_null_restores_original_text(tmp_path: Path) -> None:
    project_path, revision = _project(tmp_path)
    first = correct_transcript(
        project_path,
        source_id="src_a",
        parent_transcript_version_id="tr_a_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": "校正"}],
        expected_revision=revision,
    )
    assert first.transcript is not None
    before_paths = set((project_path / "transcripts/src_a").glob("*.json"))
    unchanged = correct_transcript(
        project_path,
        source_id="src_a",
        parent_transcript_version_id=first.transcript.transcript_version_id,
        corrections=[{"segment_id": "seg_1", "corrected_text": " 校正 "}],
        expected_revision=first.project_revision,
    )
    assert unchanged.changed is False
    assert unchanged.transcript == first.transcript
    assert unchanged.project_revision == first.project_revision
    assert set((project_path / "transcripts/src_a").glob("*.json")) == before_paths

    restored = correct_transcript(
        project_path,
        source_id="src_a",
        parent_transcript_version_id=first.transcript.transcript_version_id,
        corrections=[{"segment_id": "seg_1", "corrected_text": None}],
        expected_revision=first.project_revision,
    )
    assert restored.transcript is not None
    assert restored.transcript.segments[0].corrected_text is None
    assert restored.project_revision == first.project_revision + 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"corrections": []},
        {"corrections": [{"segment_id": "seg_unknown", "corrected_text": "x"}]},
        {
            "corrections": [
                {"segment_id": "seg_1", "corrected_text": "x"},
                {"segment_id": "seg_1", "corrected_text": "y"},
            ]
        },
        {"corrections": [{"segment_id": "seg_1", "corrected_text": "   "}]},
        {"source_id": "src_unknown"},
        {"source_id": "src_b"},
        {"parent_transcript_version_id": "tr_unknown"},
        {"parent_transcript_version_id": "../tr_a_root"},
        {"expected_revision": 0},
    ],
)
def test_invalid_corrections_leave_no_child_or_project_change(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    project_path, revision = _project(tmp_path)
    before_project = (project_path / "project.json").read_bytes()
    before_paths = set((project_path / "transcripts/src_a").glob("*.json"))
    arguments: dict[str, object] = {
        "source_id": "src_a",
        "parent_transcript_version_id": "tr_a_root",
        "corrections": [{"segment_id": "seg_1", "corrected_text": "校正"}],
        "expected_revision": revision,
        **overrides,
    }
    with pytest.raises(ProjectError):
        correct_transcript(project_path, **arguments)  # type: ignore[arg-type]
    assert (project_path / "project.json").read_bytes() == before_project
    assert set((project_path / "transcripts/src_a").glob("*.json")) == before_paths


def test_project_save_failure_rolls_back_child_and_inherited_maps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, revision = _project(tmp_path)
    project_before = (project_path / "project.json").read_bytes()
    parent_before = (project_path / "transcripts/src_a/tr_a_root.json").read_bytes()
    original_save = ProjectStore.save

    def fail_child_save(self, project, *, expected_revision):  # type: ignore[no-untyped-def]
        if project.active_transcript_versions.get("src_a") != "tr_a_root":
            raise OSError("injected project save failure")
        return original_save(self, project, expected_revision=expected_revision)

    monkeypatch.setattr(ProjectStore, "save", fail_child_save)
    with pytest.raises(OSError, match="injected"):
        correct_transcript(
            project_path,
            source_id="src_a",
            parent_transcript_version_id="tr_a_root",
            corrections=[{"segment_id": "seg_1", "corrected_text": "校正"}],
            expected_revision=revision,
        )

    assert (project_path / "project.json").read_bytes() == project_before
    assert (project_path / "transcripts/src_a/tr_a_root.json").read_bytes() == parent_before
    assert list((project_path / "transcripts/src_a").glob("*.json")) == [
        project_path / "transcripts/src_a/tr_a_root.json"
    ]


def test_activation_is_revisioned_idempotent_and_never_rewrites_transcripts(tmp_path: Path) -> None:
    project_path, revision = _project(tmp_path)
    child = _transcript("src_a", "tr_a_child", parent_id="tr_a_root", first_correction="校正")
    child_path = _write_transcript(project_path, child)
    root_path = project_path / "transcripts/src_a/tr_a_root.json"
    before = {root_path: root_path.read_bytes(), child_path: child_path.read_bytes()}

    activated = activate_transcript_version(
        project_path,
        source_id="src_a",
        transcript_version_id="tr_a_child",
        expected_revision=revision,
    )
    assert activated.changed is True
    assert activated.project_revision == revision + 1
    repeated = activate_transcript_version(
        project_path,
        source_id="src_a",
        transcript_version_id="tr_a_child",
        expected_revision=revision + 1,
    )
    assert repeated.changed is False
    assert repeated.project_revision == revision + 1
    assert {path: path.read_bytes() for path in before} == before

    with pytest.raises(ProjectError):
        activate_transcript_version(
            project_path,
            source_id="src_b",
            transcript_version_id="tr_a_child",
            expected_revision=revision + 1,
        )


@pytest.mark.parametrize("surface", ("cli", "mcp"))
@pytest.mark.parametrize("operation", ("correct", "activate"))
def test_public_transcript_changes_return_after_exact_run_binding_sync(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    surface: str,
    operation: str,
) -> None:
    case = tmp_path / f"{surface}-{operation}"
    case.mkdir()
    project_path, revision = _project(case)
    run_id = _start_scope_run(project_path, ["src_a"])
    original_run = WorkflowStore(project_path).read_run(run_id)
    expected_revision = ProjectStore(project_path).load().revision
    if operation == "correct":
        command = [
            "transcript-correct",
            "--project",
            str(project_path),
            "--source-id",
            "src_a",
            "--parent-transcript-id",
            "tr_a_root",
            "--corrections-json",
            '[{"segment_id":"seg_1","corrected_text":"公开校正"}]',
            "--expected-revision",
            str(expected_revision),
            "--json",
        ]
        arguments: dict[str, object] = {
            "project_path": str(project_path),
            "source_id": "src_a",
            "parent_transcript_version_id": "tr_a_root",
            "corrections": [{"segment_id": "seg_1", "corrected_text": "公开校正"}],
            "expected_revision": expected_revision,
        }
        tool_name = "transcript_correct"
    else:
        child = _transcript(
            "src_a", "tr_a_child", parent_id="tr_a_root", first_correction="公开校正"
        )
        _write_transcript(project_path, _workflow_hashable_transcript(child))
        command = [
            "transcript-version-activate",
            "--project",
            str(project_path),
            "--source-id",
            "src_a",
            "--transcript-id",
            child.transcript_version_id,
            "--expected-revision",
            str(expected_revision),
            "--json",
        ]
        arguments = {
            "project_path": str(project_path),
            "source_id": "src_a",
            "transcript_version_id": child.transcript_version_id,
            "expected_revision": expected_revision,
        }
        tool_name = "transcript_version_activate"

    if surface == "cli":
        roughcut.cli.main(command)
        payload = json.loads(capsys.readouterr().out)
    else:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": f"{surface}-{operation}",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )
        assert response is not None
        payload = response["result"]["structuredContent"]
    assert payload["ok"] is True
    if operation == "correct":
        transcript_id = payload["transcript_mutation"]["transcript"][
            "transcript_version_id"
        ]
    else:
        transcript_id = payload["transcript_activation"]["transcript_version_id"]
    binding = _binding(run_id, project_path, "src_a")
    assert binding.transcript_version_id == transcript_id
    assert binding.transcript_content_hash is not None
    assert WorkflowStore(project_path).read_run(run_id).stage == original_run.stage
    assert WorkflowStore(project_path).read_run(run_id).approval_refs == original_run.approval_refs
    assert workflow_status(project_path, run_id)["binding_sync"] == {
        "state": "unchanged",
        "source_ids": [],
    }
    assert ProjectStore(project_path).load().revision == expected_revision + 1
    del revision


def test_transcript_change_without_active_run_keeps_legacy_project_compatible(
    tmp_path: Path,
) -> None:
    project_path, revision = _project(tmp_path)
    changed = correct_transcript(
        project_path,
        source_id="src_a",
        parent_transcript_version_id="tr_a_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": "兼容校正"}],
        expected_revision=revision,
    )
    assert changed.changed is True
    assert not (project_path / "workflow").exists()


@pytest.mark.parametrize("surface", ("cli", "mcp"))
def test_public_transcript_change_without_active_run_keeps_legacy_compatibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    case = tmp_path / surface
    case.mkdir()
    project_path, revision = _project(case)
    arguments: dict[str, object] = {
        "project_path": str(project_path),
        "source_id": "src_a",
        "parent_transcript_version_id": "tr_a_root",
        "corrections": [{"segment_id": "seg_1", "corrected_text": "公开兼容"}],
        "expected_revision": revision,
    }
    if surface == "cli":
        roughcut.cli.main(
            [
                "transcript-correct",
                "--project",
                str(project_path),
                "--source-id",
                "src_a",
                "--parent-transcript-id",
                "tr_a_root",
                "--corrections-json",
                '[{"segment_id":"seg_1","corrected_text":"公开兼容"}]',
                "--expected-revision",
                str(revision),
                "--json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
    else:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": "legacy-transcript-change",
                "method": "tools/call",
                "params": {"name": "transcript_correct", "arguments": arguments},
            }
        )
        assert response is not None
        payload = response["result"]["structuredContent"]
    assert payload["ok"] is True
    assert not (project_path / "workflow").exists()


@pytest.mark.parametrize("surface", ("cli", "mcp"))
def test_public_transcript_sync_failure_is_not_reported_as_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    case = tmp_path / surface
    case.mkdir()
    project_path, revision = _project(case)
    run_id = _start_scope_run(project_path, ["src_a"])
    before = _binding(run_id, project_path, "src_a")
    sync_error = WorkflowError(
        "workflow_binding_sync_failed",
        "Roughcut workflow façade injected synchronization failure",
    )
    monkeypatch.setattr(
        transcripts_module,
        "synchronize_active_workflow_transcript_binding",
        lambda *_args: (_ for _ in ()).throw(sync_error),
    )
    arguments: dict[str, object] = {
        "project_path": str(project_path),
        "source_id": "src_a",
        "parent_transcript_version_id": "tr_a_root",
        "corrections": [{"segment_id": "seg_1", "corrected_text": "失败校正"}],
        "expected_revision": revision,
    }
    if surface == "cli":
        with pytest.raises(SystemExit):
            roughcut.cli.main(
                [
                    "transcript-correct",
                    "--project",
                    str(project_path),
                    "--source-id",
                    "src_a",
                    "--parent-transcript-id",
                    "tr_a_root",
                    "--corrections-json",
                    '[{"segment_id":"seg_1","corrected_text":"失败校正"}]',
                    "--expected-revision",
                    str(revision),
                    "--json",
                ]
            )
        payload = json.loads(capsys.readouterr().out)
    else:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": "transcript-sync-failure",
                "method": "tools/call",
                "params": {"name": "transcript_correct", "arguments": arguments},
            }
        )
        assert response is not None
        result = response["result"]
        assert result["isError"] is True
        payload = result["structuredContent"]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "workflow_binding_sync_failed"
    assert ProjectStore(project_path).load().active_transcript_versions["src_a"] != "tr_a_root"
    assert _binding(run_id, project_path, "src_a") == before


def test_out_of_scope_transcript_change_does_not_mutate_active_run(
    tmp_path: Path,
) -> None:
    project_path, revision = _project(tmp_path)
    run_id = _start_scope_run(project_path, ["src_a"])
    before = WorkflowStore(project_path).read_run(run_id).to_dict()
    changed = correct_transcript(
        project_path,
        source_id="src_b",
        parent_transcript_version_id="tr_b_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": "范围外校正"}],
        expected_revision=ProjectStore(project_path).load().revision,
    )
    assert changed.changed is True
    assert WorkflowStore(project_path).read_run(run_id).to_dict() == before
    assert ProjectStore(project_path).load().revision == revision + 1


def test_transcript_noop_does_not_sync_or_change_unrelated_run_state(
    tmp_path: Path,
) -> None:
    project_path, revision = _project(tmp_path)
    run_id = _start_scope_run(project_path, ["src_a"])
    before_project = (project_path / "project.json").read_bytes()
    before_run = WorkflowStore(project_path).read_run(run_id).to_dict()
    unchanged = correct_transcript(
        project_path,
        source_id="src_a",
        parent_transcript_version_id="tr_a_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": None}],
        expected_revision=ProjectStore(project_path).load().revision,
    )
    repeated = activate_transcript_version(
        project_path,
        source_id="src_a",
        transcript_version_id="tr_a_root",
        expected_revision=unchanged.project_revision,
    )
    assert unchanged.changed is False
    assert repeated.changed is False
    assert (project_path / "project.json").read_bytes() == before_project
    assert WorkflowStore(project_path).read_run(run_id).to_dict() == before_run
    del revision


def test_status_repairs_transcript_correction_after_sync_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, _revision = _project(tmp_path)
    run_id = _start_scope_run(project_path, ["src_a"])
    before = _binding(run_id, project_path, "src_a")
    monkeypatch.setattr(
        transcripts_module,
        "synchronize_active_workflow_transcript_binding",
        lambda *_args: (_ for _ in ()).throw(
            WorkflowError(
                "workflow_binding_sync_failed",
                "Roughcut workflow façade injected synchronization interruption",
            )
        ),
    )
    with pytest.raises(WorkflowError, match="Roughcut workflow façade") as error:
        correct_transcript(
            project_path,
            source_id="src_a",
            parent_transcript_version_id="tr_a_root",
            corrections=[{"segment_id": "seg_1", "corrected_text": "中断校正"}],
            expected_revision=ProjectStore(project_path).load().revision,
        )
    assert error.value.code == "workflow_binding_sync_failed"
    active_id = ProjectStore(project_path).load().active_transcript_versions["src_a"]
    assert active_id != before.transcript_version_id
    assert _binding(run_id, project_path, "src_a") == before
    monkeypatch.undo()
    repaired = workflow_status(project_path, run_id)
    assert repaired["binding_sync"] == {"state": "repaired", "source_ids": ["src_a"]}
    assert _binding(run_id, project_path, "src_a").transcript_version_id == active_id


def test_activation_rejects_stale_unknown_and_corrupt_targets(tmp_path: Path) -> None:
    project_path, revision = _project(tmp_path)
    child_path = _write_transcript(
        project_path,
        _transcript("src_a", "tr_a_child", parent_id="tr_a_root", first_correction="校正"),
    )
    with pytest.raises(ProjectError, match="revision conflict"):
        activate_transcript_version(
            project_path,
            source_id="src_a",
            transcript_version_id="tr_a_child",
            expected_revision=revision - 1,
        )
    with pytest.raises(ProjectError, match="missing or unreadable"):
        activate_transcript_version(
            project_path,
            source_id="src_a",
            transcript_version_id="tr_unknown",
            expected_revision=revision,
        )
    child_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ProjectError, match="missing or unreadable"):
        activate_transcript_version(
            project_path,
            source_id="src_a",
            transcript_version_id="tr_a_child",
            expected_revision=revision,
        )
    assert ProjectStore(project_path).load().revision == revision


def test_activation_and_correction_reject_an_invalid_source_version_graph(
    tmp_path: Path,
) -> None:
    project_path, revision = _project(tmp_path)
    orphan = _transcript(
        "src_a",
        "tr_a_orphan",
        parent_id="tr_missing",
        first_correction="孤立版本",
    )
    _write_transcript(project_path, orphan)

    with pytest.raises(ProjectError, match="parent is missing"):
        activate_transcript_version(
            project_path,
            source_id="src_a",
            transcript_version_id="tr_a_orphan",
            expected_revision=revision,
        )
    assert ProjectStore(project_path).load().revision == revision

    project = ProjectStore(project_path).load()
    ProjectStore(project_path).save(
        replace(
            project,
            revision=revision + 1,
            active_transcript_versions={
                **project.active_transcript_versions,
                "src_a": "tr_a_orphan",
            },
        ),
        expected_revision=revision,
    )
    before_paths = set((project_path / "transcripts/src_a").glob("*.json"))
    with pytest.raises(ProjectError, match="parent is missing"):
        correct_transcript(
            project_path,
            source_id="src_a",
            parent_transcript_version_id="tr_a_orphan",
            corrections=[{"segment_id": "seg_1", "corrected_text": "不得派生"}],
            expected_revision=revision + 1,
        )
    assert ProjectStore(project_path).load().revision == revision + 1
    assert set((project_path / "transcripts/src_a").glob("*.json")) == before_paths


def test_versions_read_validates_chain_without_using_mtime(tmp_path: Path) -> None:
    project_path, revision = _project(tmp_path)
    child = _transcript("src_a", "tr_a_child", parent_id="tr_a_root", first_correction="校正")
    _write_transcript(project_path, child)
    grandchild = _transcript(
        "src_a", "tr_a_grandchild", parent_id="tr_a_child", first_correction="再校正"
    )
    _write_transcript(project_path, grandchild)

    state = read_transcript_versions(project_path, source_id="src_a")
    assert state.project_revision == revision
    assert state.active_transcript_version_id == "tr_a_root"
    assert [version.to_dict() for version in state.versions] == [
        {
            "transcript_version_id": "tr_a_child",
            "parent_version_id": "tr_a_root",
            "segment_count": 2,
            "active": False,
            "kind": "user_correction",
        },
        {
            "transcript_version_id": "tr_a_grandchild",
            "parent_version_id": "tr_a_child",
            "segment_count": 2,
            "active": False,
            "kind": "user_correction",
        },
        {
            "transcript_version_id": "tr_a_root",
            "parent_version_id": None,
            "segment_count": 2,
            "active": True,
            "kind": "original_asr",
        },
    ]
    assert state.edit_reference_status.to_dict() == {"status": "none", "mismatches": []}


@pytest.mark.parametrize("failure", ["self", "missing", "cycle", "wrong-source", "corrupt"])
def test_versions_read_rejects_invalid_chains(tmp_path: Path, failure: str) -> None:
    project_path, _revision = _project(tmp_path)
    if failure == "self":
        _write_transcript(project_path, _transcript("src_a", "tr_self", parent_id="tr_self"))
    elif failure == "missing":
        _write_transcript(project_path, _transcript("src_a", "tr_child", parent_id="tr_missing"))
    elif failure == "cycle":
        _write_transcript(project_path, _transcript("src_a", "tr_left", parent_id="tr_right"))
        _write_transcript(project_path, _transcript("src_a", "tr_right", parent_id="tr_left"))
    elif failure == "wrong-source":
        _write_transcript(project_path, _transcript("src_b", "tr_b_parent"))
        _write_transcript(project_path, _transcript("src_a", "tr_child", parent_id="tr_b_parent"))
    else:
        (project_path / "transcripts/src_a/tr_corrupt.json").write_text("not json", encoding="utf-8")

    with pytest.raises(ProjectError):
        read_transcript_versions(project_path, source_id="src_a")


def test_schema_one_edit_becomes_stale_and_current_again(tmp_path: Path) -> None:
    project_path, _revision = _project(tmp_path)
    _install_schema_one_decision(project_path)
    current = read_transcript_versions(project_path, source_id="src_a")
    assert current.edit_reference_status.to_dict() == {"status": "current", "mismatches": []}

    corrected = correct_transcript(
        project_path,
        source_id="src_a",
        parent_transcript_version_id="tr_a_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": "校正"}],
        expected_revision=current.project_revision,
    )
    assert corrected.transcript is not None
    stale = read_transcript_versions(project_path, source_id="src_a")
    assert stale.edit_reference_status.to_dict() == {
        "status": "stale",
        "mismatches": [
            {
                "source_id": "src_a",
                "referenced_transcript_version_id": "tr_a_root",
                "active_transcript_version_id": corrected.transcript.transcript_version_id,
            }
        ],
    }
    restored = activate_transcript_version(
        project_path,
        source_id="src_a",
        transcript_version_id="tr_a_root",
        expected_revision=corrected.project_revision,
    )
    assert restored.project_revision == corrected.project_revision + 1
    assert read_transcript_versions(
        project_path, source_id="src_a"
    ).edit_reference_status.status == "current"


def test_schema_one_edit_ignores_version_change_on_unreferenced_source(tmp_path: Path) -> None:
    project_path, _revision = _project(tmp_path)
    _install_schema_one_decision(project_path)
    state = read_transcript_versions(project_path, source_id="src_b")
    corrected = correct_transcript(
        project_path,
        source_id="src_b",
        parent_transcript_version_id="tr_b_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": "只改 B"}],
        expected_revision=state.project_revision,
    )
    assert corrected.changed is True
    assert read_transcript_versions(
        project_path, source_id="src_a"
    ).edit_reference_status.status == "current"


def test_schema_two_edit_reports_only_actual_mismatches(tmp_path: Path) -> None:
    project_path, _revision = _project(tmp_path)
    _install_schema_two_decision(project_path)
    state = read_transcript_versions(project_path, source_id="src_a")
    corrected = correct_transcript(
        project_path,
        source_id="src_a",
        parent_transcript_version_id="tr_a_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": "校正 A"}],
        expected_revision=state.project_revision,
    )
    stale = read_transcript_versions(project_path, source_id="src_b")
    assert [item.source_id for item in stale.edit_reference_status.mismatches] == ["src_a"]

    unrelated = correct_transcript(
        project_path,
        source_id="src_b",
        parent_transcript_version_id="tr_b_root",
        corrections=[{"segment_id": "seg_1", "corrected_text": "校正 B"}],
        expected_revision=corrected.project_revision,
    )
    assert unrelated.project_revision == corrected.project_revision + 1
    mismatches = read_transcript_versions(
        project_path, source_id="src_a"
    ).edit_reference_status.mismatches
    assert [item.source_id for item in mismatches] == ["src_a", "src_b"]


@pytest.mark.parametrize("payload", [None, {"schema_version": 99}, {"schema_version": 1}])
def test_active_edit_missing_unknown_or_corrupt_is_an_error(
    tmp_path: Path, payload: dict[str, object] | None
) -> None:
    project_path, revision = _project(tmp_path)
    project = ProjectStore(project_path).load()
    ProjectStore(project_path).save(
        replace(project, revision=revision + 1, active_edit_version_id="edit_bad"),
        expected_revision=revision,
    )
    if payload is not None:
        edit_path = project_path / "edits/edit_bad.json"
        edit_path.parent.mkdir(exist_ok=True)
        edit_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProjectError):
        read_transcript_versions(project_path, source_id="src_a")


def test_cli_and_mcp_read_the_same_real_version_fixture(tmp_path: Path) -> None:
    project_path, _revision = _project(tmp_path)
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "transcript-versions-read",
            "--project",
            str(project_path),
            "--source-id",
            "src_a",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0
    assert cli.stderr == ""
    cli_payload = json.loads(cli.stdout)
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "transcript_versions_read",
                "arguments": {"project_path": str(project_path), "source_id": "src_a"},
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"] == cli_payload
