"""Versioned JSON command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from roughcut.adapters.ffmpeg.proxy import (
    FFmpegProxyError,
    ProxyCancelled,
    ProxyUnsupported,
)
from roughcut.adapters.ffmpeg.render import FFmpegRenderError, RenderCancelled
from roughcut.adapters.ffmpeg.verify import RenderVerificationError
from roughcut.adapters.funasr.normalize import TranscriptNormalizationError
from roughcut.adapters.funasr.runner import (
    FunASRRunnerError,
)
from roughcut.adapters.qwen_credential_store import QwenCredentialError
from roughcut.application.agent_context import (
    BriefState,
    create_edit_brief,
    read_agent_context,
    read_edit_brief,
    read_multi_source_agent_context,
    read_revision_context,
)
from roughcut.application.alignments import run_align_multicam
from roughcut.application.content_drafts import (
    confirm_content_draft,
    create_content_draft,
    propose_content_draft,
    read_content_draft,
    revise_content_draft_scoped,
)
from roughcut.application.diagnostics import diagnostics
from roughcut.application.draft_workspaces import read_draft_workspace
from roughcut.application.edits import (
    EditChangeState,
    EditHistoryState,
    EditNavigationState,
    change_edit,
    read_edit_history,
    redo_edit,
    undo_edit,
)
from roughcut.application.fake_projects import fake_project_roundtrip
from roughcut.application.health import health
from roughcut.application.media_operations import (
    MediaOperationOutcome,
    media_operation_status,
    run_approve_export_operation,
    run_proxy_operation,
    run_transcription_operation,
)
from roughcut.application.multicam_parallel import (
    prepare_multicam_parallel_render,
    start_multicam_parallel_render,
)
from roughcut.application.nle_handoff import approve_nle_export
from roughcut.application.people import (
    PeopleMutation,
    PeopleState,
    confirm_speaker_map,
    create_person,
    read_people,
    update_source_metadata,
)
from roughcut.application.projects import create_project, open_project
from roughcut.application.proposals import (
    DecisionReadState,
    DecisionState,
    MultiSourceDecisionState,
    MultiSourceProposalState,
    ProposalDiffState,
    ProposalState,
    confirm_edit_proposal,
    confirm_multi_source_edit_proposal,
    create_edit_proposal,
    create_multi_source_edit_proposal,
    read_decision,
    read_edit_decision,
    read_multi_source_edit_decision,
    read_proposal_diff,
    reject_edit_proposal,
    reject_multi_source_edit_proposal,
)
from roughcut.application.protected_writes import protected_write
from roughcut.application.proxies import ProxyResult, ProxyState, read_proxy
from roughcut.application.qwen_credentials import (
    clear_qwen_credential,
    configure_qwen_credential,
    public_credential_error_code,
    qwen_credential_readiness,
)
from roughcut.application.readable_transcripts import (
    export_markdown,
    read_readable_transcript,
    resolve_transcript_selection,
)
from roughcut.application.renders import RenderResult, render_roughcut
from roughcut.application.sources import add_source
from roughcut.application.transcription import read_transcript_page
from roughcut.application.transcripts import (
    TranscriptMutation,
    TranscriptVersionActivation,
    TranscriptVersionsState,
    activate_transcript_version,
    correct_transcript,
    read_transcript_versions,
)
from roughcut.application.workflows import (
    WorkflowFacadeResult,
    validate_workflow_proposal_ref,
    workflow_action,
    workflow_cancel,
    workflow_start,
    workflow_status,
)
from roughcut.domain.alignment import AlignmentError
from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import (
    MediaOperationError,
    MediaOperationRecord,
    ProxyOperationResult,
    TranscriptOperationResult,
)
from roughcut.domain.multicam_parallel import ParallelRenderError
from roughcut.domain.nle_handoff import NleHandoffError
from roughcut.domain.project import ImportMode, Project, ProjectError
from roughcut.domain.transcript import TimedTranscript
from roughcut.domain.workflow import (
    WORKFLOW_STAGES,
    ArtifactRef,
    WorkflowBinding,
    load_closed_json,
    validate_safe_id,
)
from roughcut.m2_7_public_capability import require_m2_7_public_capability
from roughcut.review.server import start_review_server


class _InvalidArgumentType(ValueError, TypeError):
    """Retain the CLI's closed ValueError envelope for type-invalid JSON."""


_ResultT = TypeVar("_ResultT")


class _UnexpectedOperationError(Exception):
    """Mark an ordinary operation error for the stable CLI envelope."""


def _call_with_exception_boundary(
    operation: Callable[[], _ResultT],
    *,
    passthrough: tuple[type[BaseException], ...],
) -> _ResultT:
    try:
        return operation()
    except Exception as error:
        if isinstance(error, passthrough):
            raise
        raise _UnexpectedOperationError from error


def _write_json(payload: dict[str, object]) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _json_error(error: str) -> NoReturn:
    payload = health()
    payload["ok"] = False
    payload["error"] = {"code": error}
    _write_json(payload)
    raise SystemExit(2)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _json_error("invalid_arguments")


_REVIEW_WORKFLOW_STAGES = frozenset(
    {"draft_review", "roughcut_review", "export_review"}
)

_QWEN_CREDENTIAL_COMMANDS = frozenset(
    {
        "qwen-credential-configure",
        "qwen-credential-readiness",
        "qwen-credential-clear",
    }
)
_CREDENTIAL_REQUEST_FIELDS = frozenset({"api_key", "workspace_id"})
_STDIN_CREDENTIAL_LIMIT_BYTES = 4096


def _read_credential_request() -> dict[str, Any]:
    """Read the one structured request that carries a secret off process argv.

    A secret passed as ``--api-key`` would land in shell history, process argv
    and diagnostic tooling, so the canonical credential configuration path
    accepts exactly one closed JSON object on stdin instead.  The read is
    bounded so an unbounded or malformed stream cannot be trusted.
    """

    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        _json_error("invalid_arguments")
    raw = stream.read(_STDIN_CREDENTIAL_LIMIT_BYTES + 1)
    if len(raw) > _STDIN_CREDENTIAL_LIMIT_BYTES:
        _json_error("invalid_arguments")
    try:
        request = load_closed_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _json_error("invalid_arguments")
    if set(request) != _CREDENTIAL_REQUEST_FIELDS:
        _json_error("invalid_arguments")
    return request


def _validate_qwen_credential_cli_options(arguments: list[str]) -> None:
    allowed = {
        "qwen-credential-configure",
        "qwen-credential-readiness",
        "qwen-credential-clear",
        "--json",
    }
    supplied = {
        argument.split("=", 1)[0]
        for argument in arguments
        if argument.startswith("--")
    }
    if not supplied <= allowed:
        _json_error("invalid_arguments")


def _review_launcher_error(code: str, evidence: str) -> WorkflowError:
    return WorkflowError(code, f"Roughcut Review launcher rejected the exact run: {evidence}")


def _review_artifact_ref(
    value: object, *, field: str, required: bool = True
) -> ArtifactRef | None:
    if value is None:
        if required:
            raise _review_launcher_error(
                "review_run_integrity", f"{field} ref is missing"
            )
        return None
    try:
        return ArtifactRef.from_dict(value)
    except (WorkflowError, ValueError, TypeError) as error:
        raise _review_launcher_error(
            "review_run_integrity", f"{field} ref is invalid: {error}"
        ) from error


def _resolve_workflow_review(project_path: Path, run_id: str) -> dict[str, Any]:
    """Resolve one exact active WorkflowRun into an existing Review surface."""

    try:
        validate_safe_id(run_id, field="run_id")
    except WorkflowError as error:
        raise _review_launcher_error("review_run_integrity", "run_id is invalid") from error

    project_manifest = project_path / "project.json"
    run_path = project_path / "workflow" / "runs" / f"{run_id}.json"
    if not project_manifest.is_file():
        raise _review_launcher_error(
            "review_run_integrity", "Project manifest is missing"
        )
    if not run_path.is_file():
        raise _review_launcher_error(
            "review_run_not_found", "the requested exact WorkflowRun is missing"
        )

    try:
        status = workflow_status(project_path, run_id)
    except WorkflowError:
        raise
    except (OSError, ProjectError, ValueError) as error:
        raise _review_launcher_error("review_run_integrity", str(error)) from error

    run = status.get("workflow_run")
    if not isinstance(run, dict) or run.get("run_id") != run_id:
        raise _review_launcher_error(
            "review_run_integrity", "status does not match the requested run_id"
        )
    lifecycle = run.get("lifecycle")
    if lifecycle in {"completed", "canceled"}:
        raise _review_launcher_error(
            "review_run_closed", f"WorkflowRun lifecycle is {lifecycle}"
        )
    if lifecycle != "active":
        raise _review_launcher_error(
            "review_run_integrity", "WorkflowRun lifecycle is unsupported"
        )
    stage = run.get("stage")
    if stage not in WORKFLOW_STAGES:
        raise _review_launcher_error(
            "review_run_integrity", "WorkflowRun stage is unsupported"
        )
    if stage not in _REVIEW_WORKFLOW_STAGES:
        raise _review_launcher_error(
            "review_stage_unsupported", f"Review does not support stage {stage!r}"
        )

    artifact_refs = run.get("artifact_refs")
    if not isinstance(artifact_refs, dict) or set(artifact_refs) != {
        "brief",
        "outline",
        "content_draft",
        "proposal",
        "decision",
        "render",
    }:
        raise _review_launcher_error(
            "review_run_integrity", "WorkflowRun artifact_refs are not closed"
        )

    if stage == "draft_review":
        raw_bindings = run.get("ordered_bindings")
        if not isinstance(raw_bindings, list) or not raw_bindings:
            raise _review_launcher_error(
                "review_run_integrity", "draft_review bindings are missing"
            )
        source_bindings: list[dict[str, object]] = []
        try:
            for raw_binding in raw_bindings:
                binding = WorkflowBinding.from_dict(raw_binding)
                if binding.transcript_version_id is None:
                    raise _review_launcher_error(
                        "review_run_integrity",
                        "draft_review binding has no exact Transcript version",
                    )
                source_bindings.append(
                    {
                        "source_id": binding.source_id,
                        "transcript_version_id": binding.transcript_version_id,
                    }
                )
        except WorkflowError:
            raise
        except (ValueError, TypeError) as error:
            raise _review_launcher_error(
                "review_run_integrity", f"draft_review binding is invalid: {error}"
            ) from error

        anchor_ref = _review_artifact_ref(
            artifact_refs["content_draft"], field="content_draft", required=False
        )
        try:
            workspace = read_draft_workspace(project_path, run_id=run_id)
        except WorkflowError as error:
            raise _review_launcher_error(
                "review_run_integrity", f"current Draft workspace is invalid: {error}"
            ) from error
        except (OSError, ProjectError, ValueError) as error:
            raise _review_launcher_error(
                "review_run_integrity", f"current Draft workspace is unreadable: {error}"
            ) from error
        current_candidate_id = (
            workspace.current_candidate.content_draft_id
            if workspace is not None
            else None if anchor_ref is None else anchor_ref.artifact_id
        )
        return {
            "proposal_id": None,
            "edit_version_id": None,
            "source_bindings": source_bindings,
            "content_draft_id": current_candidate_id,
        }

    if stage == "roughcut_review":
        proposal_ref = _review_artifact_ref(
            artifact_refs["proposal"], field="proposal"
        )
        assert proposal_ref is not None
        try:
            validate_workflow_proposal_ref(project_path, proposal_ref)
        except WorkflowError:
            raise
        except (OSError, ProjectError, ValueError) as error:
            raise _review_launcher_error(
                "review_run_integrity", f"current Proposal is unreadable: {error}"
            ) from error
        return {"proposal_id": proposal_ref.artifact_id, "edit_version_id": None}

    decision_ref = _review_artifact_ref(artifact_refs["decision"], field="decision")
    assert decision_ref is not None
    try:
        decision = read_decision(project_path, decision_ref.artifact_id).decision
    except (OSError, ProjectError, ValueError) as error:
        raise _review_launcher_error(
            "review_run_integrity", f"current Decision is unreadable: {error}"
        ) from error
    if decision.edit_version_id != decision_ref.artifact_id:
        raise _review_launcher_error(
            "review_run_integrity", "current Decision identity does not match its ref"
        )
    return {"proposal_id": None, "edit_version_id": decision_ref.artifact_id}


def main(argv: list[str] | None = None) -> None:
    argument_tokens = list(sys.argv[1:] if argv is None else argv)
    parser = JsonArgumentParser(prog="roughcut", add_help=False)
    parser.add_argument("command", nargs="?")
    parser.add_argument("project_argument", nargs="?")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--project-name")
    parser.add_argument("--project")
    parser.add_argument("--name")
    parser.add_argument("--output-preset", choices=["landscape_1080p", "portrait_1080p"])
    parser.add_argument("--source")
    parser.add_argument("--import-mode", choices=[mode.value for mode in ImportMode])
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--source-id")
    parser.add_argument("--transcript-id")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--speaker-diarization", choices=["true", "false"])
    parser.add_argument("--brief-id")
    parser.add_argument("--theme")
    parser.add_argument("--target-duration-ticks", type=int)
    parser.add_argument("--focus", action="append")
    parser.add_argument("--allow-reorder", choices=["true", "false"])
    parser.add_argument("--context-hash")
    parser.add_argument("--clips-json")
    parser.add_argument("--total-duration-ticks", type=int)
    parser.add_argument("--proposal-id")
    parser.add_argument("--edit-version-id")
    parser.add_argument("--person-id")
    parser.add_argument("--role")
    parser.add_argument("--note")
    parser.add_argument("--display-name")
    parser.add_argument("--tags-json")
    parser.add_argument("--local-speaker-id")
    parser.add_argument("--confirmed-by-user", choices=["true", "false"])
    parser.add_argument("--source-bindings-json")
    parser.add_argument("--parent-transcript-id")
    parser.add_argument("--corrections-json")
    parser.add_argument("--base-edit-version-id")
    parser.add_argument("--operation-json")
    parser.add_argument("--operation-id")
    parser.add_argument("--filters-json")
    parser.add_argument("--overlay-json")
    parser.add_argument("--view-hash")
    parser.add_argument("--selections-json")
    parser.add_argument("--basis", choices=["transcript", "proposal", "decision"])
    parser.add_argument("--artifact-id")
    parser.add_argument("--output")
    parser.add_argument("--content-draft-id")
    parser.add_argument("--parent-draft-id")
    parser.add_argument("--display-title")
    parser.add_argument("--blocks-json")
    parser.add_argument("--mutable-block-ids-json")
    parser.add_argument("--run-id")
    parser.add_argument("--action-id")
    parser.add_argument("--action")
    parser.add_argument("--input-json")
    parser.add_argument("--ordered-source-ids-json")
    parser.add_argument("--alignment-id")
    parser.add_argument("--main-camera-json")
    parser.add_argument("--auxiliary-cameras-json")
    parser.add_argument("--main-audio-stable", choices=["true"])
    parser.add_argument("--max-temporary-disk-bytes", type=int)
    parser.add_argument("--max-analysis-memory-bytes", type=int)
    parser.add_argument("--max-runtime-seconds", type=int)
    parser.add_argument("--alignment-ref-json")
    parser.add_argument("--auxiliary-camera-ids-json")
    parser.add_argument("--prepare-ref-json")
    parser.add_argument("--route", choices=["fcpxml", "fcp7_xml"])
    parser.add_argument("--destination")
    parser.add_argument("--alignment-artifact-id")
    args = parser.parse_args(argument_tokens)

    if args.command == "review":
        if args.project_argument is None:
            _json_error("invalid_input")
        if args.run_id is not None:
            if any(
                value is not None
                for value in (
                    args.proposal_id,
                    args.edit_version_id,
                    args.source_bindings_json,
                    args.content_draft_id,
                )
            ):
                _json_error("invalid_arguments")
            try:
                review = start_review_server(
                    Path(args.project_argument), **_resolve_workflow_review(
                        Path(args.project_argument), args.run_id
                    )
                )
            except WorkflowError as error:
                _json_error(error.code)
            except (OSError, ProjectError, ValueError):
                _json_error("review_run_integrity")
        else:
            workflow_bindings: list[dict[str, Any]] | None = None
            if args.source_bindings_json is not None:
                if args.proposal_id is not None or args.edit_version_id is not None:
                    _json_error("invalid_arguments")
                try:
                    workflow_bindings = _parse_object_array_json(args.source_bindings_json)
                except (json.JSONDecodeError, ValueError):
                    _json_error("invalid_arguments")
            elif args.content_draft_id is not None:
                _json_error("invalid_arguments")
            try:
                if workflow_bindings is None:
                    review = start_review_server(
                        Path(args.project_argument),
                        proposal_id=args.proposal_id,
                        edit_version_id=args.edit_version_id,
                    )
                else:
                    review = start_review_server(
                        Path(args.project_argument),
                        proposal_id=None,
                        edit_version_id=None,
                        source_bindings=workflow_bindings,
                        content_draft_id=args.content_draft_id,
                    )
            except (OSError, ProjectError):
                _json_error("review_operation_failed")
        if args.json:
            payload = health()
            payload["review"] = {"url": review.url}
            _write_json(payload)
        else:
            print(f"Review: {review.url}", flush=True)
        try:
            review.wait()
        except KeyboardInterrupt:
            pass
        finally:
            review.close()
        return
    if args.project_argument is not None:
        _json_error("invalid_arguments")
    if not args.json:
        _json_error("json_output_required")
    if args.command == "health":
        _write_json(health())
        return
    if args.command == "diagnostics":
        _write_json(diagnostics())
        return
    if args.command in _QWEN_CREDENTIAL_COMMANDS:
        _validate_qwen_credential_cli_options(argument_tokens)
        try:
            if args.command == "qwen-credential-configure":
                request = _read_credential_request()
                credential = configure_qwen_credential(
                    api_key=request["api_key"],
                    workspace_id=request["workspace_id"],
                )
            elif args.command == "qwen-credential-clear":
                credential = clear_qwen_credential()
            else:
                credential = qwen_credential_readiness()
        except QwenCredentialError as error:
            _json_error(public_credential_error_code(error))
        payload = health()
        payload["credential"] = credential
        _write_json(payload)
        return
    if args.command == "approve-nle-export":
        _validate_nle_export_cli_options(argument_tokens)
        if (
            args.project is None
            or args.run_id is None
            or args.action_id is None
            or args.edit_version_id is None
            or args.expected_revision is None
            or args.route is None
            or args.destination is None
        ):
            _json_error("invalid_arguments")
        try:
            outcome = approve_nle_export(
                Path(args.project),
                run_id=args.run_id,
                action_id=args.action_id,
                edit_version_id=args.edit_version_id,
                expected_revision=args.expected_revision,
                route=args.route,
                destination=args.destination,
                alignment_artifact_id=args.alignment_artifact_id,
            )
            payload = health()
            payload.update(outcome.to_dict())
            _write_json(payload)
            return
        except NleHandoffError as error:
            _json_error(error.code)
        except (OSError, ProjectError, ValueError):
            _json_error("nle_export_integrity_error")
    if args.command == "media-operation-status":
        supplied = {
            argument.split("=", 1)[0]
            for argument in argument_tokens[1:]
            if argument.startswith("--")
        }
        if supplied - {"--project", "--operation-id", "--json"}:
            _json_error("invalid_arguments")
        if not args.project or not args.operation_id:
            _json_error("invalid_arguments")
        try:
            operation = media_operation_status(
                Path(args.project), args.operation_id
            )
            _write_json(_media_operation_status_payload(operation))
            return
        except MediaOperationError as error:
            _json_error(error.code)
        except (OSError, ProjectError, ValueError):
            _json_error("operation_integrity_error")
    if args.command in {
        "align-multicam",
        "multicam-parallel-render-prepare",
        "multicam-parallel-render-start",
    }:
        _validate_media_start_cli_options(argument_tokens, args.command)
        try:
            if args.project is None:
                _json_error("invalid_arguments")
            if args.command == "align-multicam":
                if (
                    args.operation_id is None
                    or args.alignment_id is None
                    or args.expected_revision is None
                    or args.main_camera_json is None
                    or args.auxiliary_cameras_json is None
                    or args.main_audio_stable is None
                    or args.max_temporary_disk_bytes is None
                    or args.max_analysis_memory_bytes is None
                    or args.max_runtime_seconds is None
                ):
                    _json_error("invalid_arguments")
                try:
                    main_camera = _parse_object_json(args.main_camera_json)
                    auxiliary_cameras = _parse_object_array_json(
                        args.auxiliary_cameras_json
                    )
                except (json.JSONDecodeError, ValueError):
                    _json_error("invalid_arguments")
                require_m2_7_public_capability("align_multicam")
                alignment_outcome = run_align_multicam(
                    Path(args.project),
                    operation_id=args.operation_id,
                    alignment_id=args.alignment_id,
                    expected_revision=args.expected_revision,
                    main_camera=main_camera,
                    auxiliary_cameras=auxiliary_cameras,
                    main_audio_stable=args.main_audio_stable == "true",
                    max_temporary_disk_bytes=args.max_temporary_disk_bytes,
                    max_analysis_memory_bytes=args.max_analysis_memory_bytes,
                    max_runtime_seconds=args.max_runtime_seconds,
                )
                _write_json(_alignment_operation_payload(alignment_outcome))
                return
            if args.command == "multicam-parallel-render-prepare":
                if (
                    args.edit_version_id is None
                    or args.alignment_ref_json is None
                    or args.auxiliary_camera_ids_json is None
                    or args.expected_revision is None
                ):
                    _json_error("invalid_arguments")
                try:
                    alignment_ref = _parse_object_json(args.alignment_ref_json)
                    auxiliary_camera_ids = _parse_string_array_json(
                        args.auxiliary_camera_ids_json
                    )
                except (json.JSONDecodeError, ValueError):
                    _json_error("invalid_arguments")
                require_m2_7_public_capability("multicam_parallel_render_prepare")
                parallel_prepare_outcome = prepare_multicam_parallel_render(
                    Path(args.project),
                    edit_version_id=args.edit_version_id,
                    alignment_ref=alignment_ref,
                    auxiliary_camera_ids=auxiliary_camera_ids,
                    expected_revision=args.expected_revision,
                )
                _write_json(_parallel_prepare_payload(parallel_prepare_outcome))
                return
            if args.operation_id is None or args.prepare_ref_json is None:
                _json_error("invalid_arguments")
            try:
                prepare_ref = _parse_object_json(args.prepare_ref_json)
            except (json.JSONDecodeError, ValueError):
                _json_error("invalid_arguments")
            require_m2_7_public_capability("multicam_parallel_render_start")
            parallel_render_outcome = start_multicam_parallel_render(
                Path(args.project),
                operation_id=args.operation_id,
                prepare_ref=prepare_ref,
            )
            _write_json(_parallel_operation_payload(parallel_render_outcome))
            return
        except (AlignmentError, MediaOperationError, ParallelRenderError) as error:
            _json_error(error.code)
        except (json.JSONDecodeError, OSError, ProjectError, ValueError):
            _json_error("operation_integrity_error")
    if args.command in {
        "workflow-start",
        "workflow-status",
        "workflow-action",
        "workflow-cancel",
    }:
        _validate_workflow_cli_options(argument_tokens, args.command)
        try:
            if not args.project:
                _json_error("invalid_arguments")
            if args.command == "workflow-start":
                if not args.run_id or args.ordered_source_ids_json is None:
                    _json_error("invalid_arguments")
                workflow_result = workflow_start(
                    Path(args.project),
                    args.run_id,
                    _parse_string_array_json(args.ordered_source_ids_json),
                )
                _write_json(_workflow_result_payload(workflow_result))
                return
            if args.command == "workflow-status":
                if args.run_id == "":
                    _json_error("invalid_arguments")
                _write_json(
                    _workflow_status_payload(
                        workflow_status(Path(args.project), args.run_id)
                    )
                )
                return
            if not args.run_id or not args.action_id:
                _json_error("invalid_arguments")
            if args.command == "workflow-action":
                if not args.action or args.input_json is None:
                    _json_error("invalid_arguments")
                try:
                    action_input = load_closed_json(args.input_json)
                except WorkflowError:
                    _json_error("workflow_action_invalid")
                if args.action == "approve_export":
                    try:
                        export_outcome = _call_with_exception_boundary(
                            lambda: run_approve_export_operation(
                                Path(args.project),
                                run_id=args.run_id,
                                action_id=args.action_id,
                                action_input=action_input,
                            ),
                            passthrough=(
                                WorkflowError,
                                MediaOperationError,
                                RenderCancelled,
                            ),
                        )
                    except (WorkflowError, MediaOperationError) as error:
                        _json_error(error.code)
                    except RenderCancelled:
                        _json_error("render_cancelled")
                    except _UnexpectedOperationError:
                        _json_error("render_operation_failed")
                    _write_json(_approve_export_operation_payload(export_outcome))
                    return
                workflow_result = workflow_action(
                    Path(args.project),
                    args.run_id,
                    args.action_id,
                    args.action,
                    action_input,
                )
            else:
                workflow_result = workflow_cancel(
                    Path(args.project), args.run_id, args.action_id
                )
            _write_json(_workflow_result_payload(workflow_result))
            return
        except (WorkflowError, MediaOperationError) as error:
            _json_error(error.code)
        except (json.JSONDecodeError, OSError, ValueError):
            _json_error("invalid_arguments")
    if args.command == "fake-project-roundtrip":
        try:
            _write_json(fake_project_roundtrip(args.project_name or ""))
        except ValueError:
            _json_error("invalid_input")
        return
    try:
        if args.command == "person-create":
            if (
                args.project is None
                or args.name is None
                or args.role is None
                or args.note is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            result = create_person(
                Path(args.project),
                name=args.name,
                role=args.role,
                note=args.note,
                expected_revision=args.expected_revision,
            )
            _write_json(_people_mutation_payload(result))
            return
        if args.command == "source-metadata-update":
            if (
                args.project is None
                or args.source_id is None
                or args.tags_json is None
                or args.note is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            result = update_source_metadata(
                Path(args.project),
                source_id=args.source_id,
                display_name=args.display_name,
                tags=_parse_tags_json(args.tags_json),
                note=args.note,
                expected_revision=args.expected_revision,
            )
            _write_json(_people_mutation_payload(result))
            return
        if args.command == "speaker-map-confirm":
            if (
                args.project is None
                or args.source_id is None
                or args.transcript_id is None
                or args.local_speaker_id is None
                or args.person_id is None
                or args.confirmed_by_user is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            result = confirm_speaker_map(
                Path(args.project),
                source_id=args.source_id,
                transcript_version_id=args.transcript_id,
                local_speaker_id=args.local_speaker_id,
                person_id=args.person_id,
                confirmed_by_user=args.confirmed_by_user == "true",
                expected_revision=args.expected_revision,
            )
            _write_json(_people_mutation_payload(result))
            return
        if args.command == "people-read":
            if args.project is None:
                _json_error("invalid_input")
            _write_json(_people_state_payload(read_people(Path(args.project))))
            return
    except (json.JSONDecodeError, OSError, ProjectError, ValueError):
        _json_error("people_operation_failed")
    if args.command == "readable-transcript-read":
        if (
            args.project is None
            or args.source_bindings_json is None
            or args.expected_revision is None
            or not _option_present(argument_tokens, "--offset")
            or not _option_present(argument_tokens, "--limit")
        ):
            _json_error("invalid_arguments")
        try:
            readable_page = read_readable_transcript(
                Path(args.project),
                source_bindings=_parse_object_array_json(args.source_bindings_json),
                expected_revision=args.expected_revision,
                offset=args.offset,
                limit=args.limit,
                filters=(
                    _parse_object_json(args.filters_json) if args.filters_json is not None else None
                ),
                overlay=(
                    _parse_object_json(args.overlay_json) if args.overlay_json is not None else None
                ),
            )
        except (json.JSONDecodeError, OSError, ProjectError, ValueError):
            _json_error("readable_transcript_failed")
        payload = health()
        payload["readable_transcript"] = readable_page.to_dict()
        _write_json(payload)
        return
    if args.command == "transcript-selection-resolve":
        if (
            args.project is None
            or args.source_bindings_json is None
            or args.expected_revision is None
            or args.view_hash is None
            or args.selections_json is None
        ):
            _json_error("invalid_arguments")
        try:
            resolution = resolve_transcript_selection(
                Path(args.project),
                source_bindings=_parse_object_array_json(args.source_bindings_json),
                expected_revision=args.expected_revision,
                view_hash=args.view_hash,
                selections=_parse_object_array_json(args.selections_json),
                overlay=(
                    _parse_object_json(args.overlay_json) if args.overlay_json is not None else None
                ),
            )
        except (json.JSONDecodeError, OSError, ProjectError, ValueError):
            _json_error("transcript_selection_failed")
        payload = health()
        payload["transcript_selection"] = resolution.to_dict()
        _write_json(payload)
        return
    if args.command == "markdown-export":
        if (
            args.project is None
            or args.expected_revision is None
            or args.basis is None
            or args.output is None
        ):
            _json_error("invalid_arguments")
        try:
            exported = export_markdown(
                Path(args.project),
                basis=args.basis,
                output_path=Path(args.output),
                expected_revision=args.expected_revision,
                source_bindings=(
                    _parse_object_array_json(args.source_bindings_json)
                    if args.source_bindings_json is not None
                    else None
                ),
                artifact_id=args.artifact_id,
            )
        except (json.JSONDecodeError, OSError, ProjectError, ValueError):
            _json_error("markdown_export_failed")
        payload = health()
        payload["markdown_export"] = exported.to_dict()
        _write_json(payload)
        return
    if args.command in {
        "content-draft-create",
        "content-draft-revise-scoped",
        "content-draft-read",
        "content-draft-confirm",
        "content-draft-propose",
    }:
        if args.project is None:
            _json_error("invalid_arguments")
        try:
            if args.command == "content-draft-create":
                if (
                    args.source_bindings_json is None
                    or args.brief_id is None
                    or args.context_hash is None
                    or args.blocks_json is None
                    or args.expected_revision is None
                ):
                    _json_error("invalid_arguments")
                with protected_write(
                    Path(args.project), "content_draft_create"
                ):
                    draft_result = create_content_draft(
                        Path(args.project),
                        parent_draft_id=args.parent_draft_id,
                        display_title=args.display_title,
                        source_bindings=_parse_object_array_json(
                            args.source_bindings_json
                        ),
                        brief_id=args.brief_id,
                        context_hash=args.context_hash,
                        blocks=_parse_object_array_json(args.blocks_json),
                        expected_revision=args.expected_revision,
                    )
                payload = health()
                payload.update(draft_result.to_dict())
                _write_json(payload)
                return
            if args.command == "content-draft-revise-scoped":
                if (
                    args.parent_draft_id is None
                    or args.mutable_block_ids_json is None
                    or args.blocks_json is None
                    or args.expected_revision is None
                ):
                    _json_error("invalid_arguments")
                with protected_write(
                    Path(args.project), "content_draft_revise_scoped"
                ):
                    revision_result = revise_content_draft_scoped(
                        Path(args.project),
                        parent_draft_id=args.parent_draft_id,
                        mutable_block_ids=_parse_tags_json(
                            args.mutable_block_ids_json
                        ),
                        blocks=_parse_object_array_json(args.blocks_json),
                        expected_revision=args.expected_revision,
                    )
                payload = health()
                payload.update(revision_result.to_dict())
                _write_json(payload)
                return
            if args.content_draft_id is None:
                _json_error("invalid_arguments")
            if args.command == "content-draft-read":
                draft_state = read_content_draft(
                    Path(args.project), args.content_draft_id
                )
                payload = health()
                payload.update(draft_state.to_dict())
                _write_json(payload)
                return
            if args.expected_revision is None:
                _json_error("invalid_arguments")
            if args.command == "content-draft-confirm":
                with protected_write(
                    Path(args.project), "content_draft_confirm"
                ):
                    draft_result = confirm_content_draft(
                        Path(args.project),
                        args.content_draft_id,
                        expected_revision=args.expected_revision,
                    )
                payload = health()
                payload.update(draft_result.to_dict())
                _write_json(payload)
                return
            with protected_write(Path(args.project), "content_draft_propose"):
                proposal_result = propose_content_draft(
                    Path(args.project),
                    args.content_draft_id,
                    expected_revision=args.expected_revision,
                )
            payload = health()
            payload.update(proposal_result.to_dict())
            _write_json(payload)
            return
        except WorkflowError as error:
            _json_error(error.code)
        except (json.JSONDecodeError, OSError, ProjectError, ValueError):
            _json_error("content_draft_operation_failed")
    if args.command == "revision-context":
        if (
            args.project is None
            or args.expected_revision is None
            or not _option_present(argument_tokens, "--offset")
            or not _option_present(argument_tokens, "--limit")
        ):
            _json_error("invalid_arguments")
        try:
            revision_context = read_revision_context(
                Path(args.project),
                expected_revision=args.expected_revision,
                offset=args.offset,
                limit=args.limit,
            )
        except (OSError, ProjectError, ValueError):
            _json_error("revision_context_failed")
        payload = health()
        payload["revision_context"] = revision_context.to_dict()
        _write_json(payload)
        return
    if args.command == "proposal-diff-read":
        if args.project is None or args.proposal_id is None or args.expected_revision is None:
            _json_error("invalid_arguments")
        try:
            proposal_diff = read_proposal_diff(
                Path(args.project),
                args.proposal_id,
                expected_revision=args.expected_revision,
            )
        except (OSError, ProjectError, ValueError):
            _json_error("proposal_diff_failed")
        _write_json(_proposal_diff_payload(proposal_diff))
        return
    try:
        if args.command == "multi-source-context":
            if (
                args.project is None
                or args.source_bindings_json is None
                or args.brief_id is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            multi_context = read_multi_source_agent_context(
                Path(args.project),
                source_bindings=_parse_object_array_json(args.source_bindings_json),
                brief_id=args.brief_id,
                expected_revision=args.expected_revision,
                offset=args.offset,
                limit=args.limit,
            )
            payload = health()
            payload["multi_source_context"] = multi_context.to_dict()
            _write_json(payload)
            return
        if args.command == "multi-source-proposal-create":
            if (
                args.project is None
                or args.source_bindings_json is None
                or args.brief_id is None
                or args.context_hash is None
                or args.clips_json is None
                or args.total_duration_ticks is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            with protected_write(
                Path(args.project), "multi_source_proposal_create"
            ):
                multi_proposal_state = create_multi_source_edit_proposal(
                    Path(args.project),
                    source_bindings=_parse_object_array_json(
                        args.source_bindings_json
                    ),
                    brief_id=args.brief_id,
                    context_hash=args.context_hash,
                    clips=_parse_clips_json(args.clips_json),
                    total_duration_ticks=args.total_duration_ticks,
                    expected_revision=args.expected_revision,
                )
            _write_json(_multi_source_proposal_payload(multi_proposal_state))
            return
        if args.command == "multi-source-proposal-confirm":
            if args.project is None or args.proposal_id is None or args.expected_revision is None:
                _json_error("invalid_input")
            with protected_write(
                Path(args.project), "multi_source_proposal_confirm"
            ):
                multi_decision_state = confirm_multi_source_edit_proposal(
                    Path(args.project),
                    args.proposal_id,
                    expected_revision=args.expected_revision,
                )
            _write_json(_multi_source_decision_payload(multi_decision_state))
            return
        if args.command == "multi-source-edit-decision-read":
            if args.project is None or args.edit_version_id is None:
                _json_error("invalid_input")
            multi_read_state = read_multi_source_edit_decision(
                Path(args.project), args.edit_version_id
            )
            _write_json(_multi_source_decision_payload(multi_read_state))
            return
    except WorkflowError as error:
        _json_error(error.code)
    except (json.JSONDecodeError, OSError, ProjectError, ValueError):
        _json_error("multi_source_operation_failed")
    try:
        if args.command == "brief-create":
            if (
                args.project is None
                or args.theme is None
                or args.target_duration_ticks is None
                or args.focus is None
                or args.allow_reorder is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            with protected_write(Path(args.project), "brief_create"):
                state = create_edit_brief(
                    Path(args.project),
                    theme=args.theme,
                    target_duration_ticks=args.target_duration_ticks,
                    focus=args.focus,
                    allow_reorder=args.allow_reorder == "true",
                    expected_revision=args.expected_revision,
                )
            _write_json(_brief_payload(state))
            return
        if args.command == "brief-read":
            if args.project is None or args.brief_id is None:
                _json_error("invalid_input")
            _write_json(_brief_payload(read_edit_brief(Path(args.project), args.brief_id)))
            return
        if args.command == "agent-context":
            if (
                args.project is None
                or args.source_id is None
                or args.transcript_id is None
                or args.brief_id is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            context = read_agent_context(
                Path(args.project),
                source_id=args.source_id,
                transcript_version_id=args.transcript_id,
                brief_id=args.brief_id,
                expected_revision=args.expected_revision,
                offset=args.offset,
                limit=args.limit,
            )
            payload = health()
            payload["agent_context"] = context.to_dict()
            _write_json(payload)
            return
    except WorkflowError as error:
        _json_error(error.code)
    except (OSError, ProjectError):
        _json_error("agent_operation_failed")
    try:
        if args.command == "proposal-create":
            if (
                args.project is None
                or args.source_id is None
                or args.transcript_id is None
                or args.brief_id is None
                or args.context_hash is None
                or args.clips_json is None
                or args.total_duration_ticks is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            with protected_write(Path(args.project), "proposal_create"):
                proposal = create_edit_proposal(
                    Path(args.project),
                    source_id=args.source_id,
                    transcript_version_id=args.transcript_id,
                    brief_id=args.brief_id,
                    context_hash=args.context_hash,
                    clips=_parse_clips_json(args.clips_json),
                    total_duration_ticks=args.total_duration_ticks,
                    expected_revision=args.expected_revision,
                )
            _write_json(_proposal_payload(proposal))
            return
        if args.command == "proposal-confirm":
            if args.project is None or args.proposal_id is None or args.expected_revision is None:
                _json_error("invalid_input")
            with protected_write(Path(args.project), "proposal_confirm"):
                decision = confirm_edit_proposal(
                    Path(args.project),
                    args.proposal_id,
                    expected_revision=args.expected_revision,
                )
            _write_json(_decision_payload(decision))
            return
        if args.command == "proposal-reject":
            if args.project is None or args.proposal_id is None or args.expected_revision is None:
                _json_error("invalid_input")
            with protected_write(
                Path(args.project),
                "proposal_reject",
                proposal_id=args.proposal_id,
            ) as run:
                current_proposal = run.artifact_refs["proposal"]
                assert current_proposal is not None
                reject = (
                    reject_multi_source_edit_proposal
                    if current_proposal.schema_version == 2
                    else reject_edit_proposal
                )
                rejection = reject(
                    Path(args.project),
                    args.proposal_id,
                    expected_revision=args.expected_revision,
                )
            payload = health()
            payload["proposal_rejection"] = rejection.to_dict()
            _write_json(payload)
            return
        if args.command == "edit-decision-read":
            if args.project is None or args.edit_version_id is None:
                _json_error("invalid_input")
            _write_json(
                _decision_payload(read_edit_decision(Path(args.project), args.edit_version_id))
            )
            return
        if args.command == "decision-read":
            if args.project is None or args.edit_version_id is None:
                _json_error("invalid_input")
            _write_json(
                _decision_read_payload(
                    read_decision(Path(args.project), args.edit_version_id)
                )
            )
            return
    except WorkflowError as error:
        _json_error(error.code)
    except (json.JSONDecodeError, OSError, ProjectError, ValueError):
        _json_error(
            "decision_read_integrity"
            if args.command == "decision-read"
            else "proposal_operation_failed"
        )
    try:
        if args.command == "edit-change":
            if (
                args.project is None
                or args.operation_json is None
                or args.expected_revision is None
                or args.base_edit_version_id is None
            ):
                _json_error("invalid_arguments")
            changed = change_edit(
                Path(args.project),
                operation=_parse_object_json(args.operation_json),
                expected_revision=args.expected_revision,
                base_edit_version_id=args.base_edit_version_id,
            )
            _write_json(_edit_change_payload(changed))
            return
        if args.command in {"edit-undo", "edit-redo"}:
            if (
                args.project is None
                or args.expected_revision is None
                or args.base_edit_version_id is None
            ):
                _json_error("invalid_arguments")
            navigation = (undo_edit if args.command == "edit-undo" else redo_edit)(
                Path(args.project),
                expected_revision=args.expected_revision,
                base_edit_version_id=args.base_edit_version_id,
            )
            _write_json(_edit_navigation_payload(navigation))
            return
        if args.command == "edit-history-read":
            if args.project is None:
                _json_error("invalid_arguments")
            _write_json(_edit_history_payload(read_edit_history(Path(args.project))))
            return
    except (OSError, ProjectError):
        _json_error("edit_operation_failed")
    except (json.JSONDecodeError, ValueError):
        _json_error("invalid_arguments")
    try:
        if args.command == "proxy-create":
            _validate_media_start_cli_options(argument_tokens, args.command)
            if (
                args.project is None
                or args.operation_id is None
                or args.source_id is None
                or args.expected_revision is None
            ):
                _json_error("invalid_arguments")
            try:
                proxy_outcome = _call_with_exception_boundary(
                    lambda: run_proxy_operation(
                        Path(args.project),
                        operation_id=args.operation_id,
                        source_id=args.source_id,
                        expected_project_revision=args.expected_revision,
                    ),
                    passthrough=(
                        WorkflowError,
                        MediaOperationError,
                        ProxyCancelled,
                        ProxyUnsupported,
                    ),
                )
            except (WorkflowError, MediaOperationError) as error:
                _json_error(error.code)
            except ProxyCancelled:
                _json_error("proxy_cancelled")
            except ProxyUnsupported:
                _json_error("proxy_unsupported")
            except _UnexpectedOperationError:
                _json_error("proxy_operation_failed")
            _write_json(_proxy_operation_payload(proxy_outcome))
            return
        if args.command == "proxy-read":
            if args.project is None or args.source_id is None or args.expected_revision is None:
                _json_error("invalid_arguments")
            _write_json(
                _proxy_payload(
                    read_proxy(
                        Path(args.project),
                        source_id=args.source_id,
                        expected_revision=args.expected_revision,
                    )
                )
            )
            return
    except MediaOperationError as error:
        _json_error(error.code)
    except (KeyboardInterrupt, ProxyCancelled):
        _json_error("proxy_cancelled")
    except ProxyUnsupported:
        _json_error("proxy_unsupported")
    except (FFmpegProxyError, OSError, ProjectError, ValueError):
        _json_error("proxy_operation_failed")
    try:
        if args.command == "render-roughcut":
            if (
                args.project is None
                or args.edit_version_id is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            with protected_write(Path(args.project), "render_roughcut"):
                rendered = render_roughcut(
                    Path(args.project),
                    edit_version_id=args.edit_version_id,
                    expected_revision=args.expected_revision,
                )
            _write_json(_render_payload(rendered))
            return
    except (KeyboardInterrupt, RenderCancelled):
        _json_error("render_cancelled")
    except WorkflowError as error:
        _json_error(error.code)
    except (FFmpegRenderError, RenderVerificationError, OSError, ProjectError):
        _json_error("render_operation_failed")
    try:
        if args.command == "transcript-correct":
            if (
                args.project is None
                or args.source_id is None
                or args.parent_transcript_id is None
                or args.corrections_json is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            mutation = correct_transcript(
                Path(args.project),
                source_id=args.source_id,
                parent_transcript_version_id=args.parent_transcript_id,
                corrections=_parse_object_array_json(args.corrections_json),
                expected_revision=args.expected_revision,
            )
            _write_json(_transcript_mutation_payload(mutation))
            return
        if args.command == "transcript-version-activate":
            if (
                args.project is None
                or args.source_id is None
                or args.transcript_id is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            activation = activate_transcript_version(
                Path(args.project),
                source_id=args.source_id,
                transcript_version_id=args.transcript_id,
                expected_revision=args.expected_revision,
            )
            _write_json(_transcript_activation_payload(activation))
            return
        if args.command == "transcript-versions-read":
            if args.project is None or args.source_id is None:
                _json_error("invalid_input")
            versions = read_transcript_versions(Path(args.project), source_id=args.source_id)
            _write_json(_transcript_versions_payload(versions))
            return
    except WorkflowError as error:
        _json_error(error.code)
    except (OSError, ProjectError):
        _json_error("transcript_version_operation_failed")
    except (json.JSONDecodeError, ValueError):
        _json_error("invalid_input")
    try:
        if args.command == "transcribe-source":
            _validate_media_start_cli_options(argument_tokens, args.command)
            if (
                args.project is None
                or args.operation_id is None
                or args.source_id is None
                or args.expected_revision is None
            ):
                _json_error("invalid_arguments")
            try:
                transcription_outcome = _call_with_exception_boundary(
                    lambda: run_transcription_operation(
                        Path(args.project),
                        operation_id=args.operation_id,
                        source_id=args.source_id,
                        expected_project_revision=args.expected_revision,
                        speaker_diarization=args.speaker_diarization == "true",
                    ),
                    passthrough=(WorkflowError, MediaOperationError),
                )
            except (WorkflowError, MediaOperationError) as error:
                _json_error(error.code)
            except _UnexpectedOperationError:
                _json_error("transcription_failed")
            _write_json(
                _transcription_operation_payload(transcription_outcome)
            )
            return
        if args.command == "transcript-page":
            if args.project is None or args.source_id is None or args.transcript_id is None:
                _json_error("invalid_input")
            page = read_transcript_page(
                Path(args.project),
                args.source_id,
                args.transcript_id,
                offset=args.offset,
                limit=args.limit,
            )
            payload = health()
            payload["transcript_page"] = page.to_dict()
            _write_json(payload)
            return
    except MediaOperationError as error:
        _json_error(error.code)
    except TranscriptNormalizationError:
        _json_error("transcription_failed")
    except WorkflowError as error:
        _json_error(error.code)
    except (FunASRRunnerError, OSError, ProjectError):
        _json_error("transcription_failed")
    except ValueError:
        _json_error("invalid_arguments")
    try:
        if args.command == "project-create":
            if args.project is None or args.name is None:
                _json_error("invalid_input")
            _write_json(
                _project_payload(
                    create_project(
                        Path(args.project),
                        args.name,
                        output_preset=args.output_preset,
                    )
                )
            )
            return
        if args.command == "project-open":
            if args.project is None:
                _json_error("invalid_input")
            _write_json(_project_payload(open_project(Path(args.project))))
            return
        if args.command == "source-add":
            if (
                args.project is None
                or args.source is None
                or args.import_mode is None
                or args.expected_revision is None
            ):
                _json_error("invalid_input")
            project = add_source(
                Path(args.project),
                Path(args.source),
                ImportMode(args.import_mode),
                expected_revision=args.expected_revision,
            )
            _write_json(_project_payload(project))
            return
    except (OSError, ProjectError):
        _json_error("project_operation_failed")

    _json_error("unknown_command")


def _project_payload(project: Project) -> dict[str, object]:
    payload = health()
    payload["project"] = project.to_dict()
    return payload


def _transcript_mutation_payload(result: TranscriptMutation) -> dict[str, object]:
    payload = health()
    payload["transcript_mutation"] = result.to_dict()
    return payload


def _transcript_activation_payload(result: TranscriptVersionActivation) -> dict[str, object]:
    payload = health()
    payload["transcript_activation"] = result.to_dict()
    return payload


def _transcript_versions_payload(result: TranscriptVersionsState) -> dict[str, object]:
    payload = health()
    payload["transcript_versions"] = result.to_dict()
    return payload


def _brief_payload(state: BriefState) -> dict[str, object]:
    payload = health()
    payload.update(state.to_dict())
    return payload


def _proposal_payload(state: ProposalState) -> dict[str, object]:
    payload = health()
    payload.update(state.to_dict())
    return payload


def _decision_payload(state: DecisionState) -> dict[str, object]:
    payload = health()
    payload.update(state.to_dict())
    return payload


def _decision_read_payload(state: DecisionReadState) -> dict[str, object]:
    payload = health()
    payload.update(state.to_dict())
    return payload


def _proposal_diff_payload(state: ProposalDiffState) -> dict[str, object]:
    payload = health()
    payload.update(state.to_dict())
    return payload


def _multi_source_proposal_payload(state: MultiSourceProposalState) -> dict[str, object]:
    payload = health()
    payload.update(state.to_dict())
    return payload


def _multi_source_decision_payload(state: MultiSourceDecisionState) -> dict[str, object]:
    payload = health()
    payload.update(state.to_dict())
    return payload


def _render_payload(result: RenderResult) -> dict[str, object]:
    payload = health()
    payload["render"] = result.to_dict()
    return payload


def _proxy_payload(result: ProxyResult | ProxyState) -> dict[str, object]:
    payload = health()
    payload["proxy"] = result.to_dict()
    return payload


def _transcription_operation_payload(
    outcome: MediaOperationOutcome[object],
) -> dict[str, object]:
    payload = health()
    payload["media_operation"] = outcome.record.to_dict()
    payload["operation_readback"] = outcome.readback
    payload["transcript"] = None
    if outcome.result is not None:
        if not isinstance(outcome.result, TimedTranscript):
            raise MediaOperationError(
                "operation_integrity_error",
                "Roughcut transcription returned an invalid public result",
            )
        result_ref = outcome.record.result_ref
        if not isinstance(result_ref, TranscriptOperationResult):
            raise MediaOperationError(
                "operation_integrity_error",
                "Roughcut transcription record lacks its exact result identity",
            )
        payload["transcript"] = {
            "transcript_version_id": outcome.result.transcript_version_id,
            "source_id": outcome.result.source_id,
            "segment_count": len(outcome.result.segments),
            "raw_result_path": outcome.result.provenance.raw_result_path,
            "project_revision": result_ref.project_revision,
        }
    return payload


def _proxy_operation_payload(
    outcome: MediaOperationOutcome[ProxyResult],
) -> dict[str, object]:
    payload = health()
    payload["media_operation"] = outcome.record.to_dict()
    payload["operation_readback"] = outcome.readback
    payload["proxy"] = None
    if outcome.result is not None:
        if not isinstance(outcome.record.result_ref, ProxyOperationResult):
            raise MediaOperationError(
                "operation_integrity_error",
                "Roughcut Proxy record lacks its exact result identity",
            )
        payload["proxy"] = outcome.result.to_dict()
    return payload


def _approve_export_operation_payload(
    outcome: MediaOperationOutcome[object],
) -> dict[str, object]:
    if not isinstance(outcome.result, WorkflowFacadeResult):
        raise MediaOperationError(
            "operation_integrity_error",
            "Roughcut approve_export returned an invalid public result",
        )
    payload = health()
    payload["media_operation"] = outcome.record.to_dict()
    payload["operation_readback"] = outcome.readback
    payload.update(outcome.result.to_dict())
    return payload


def _edit_change_payload(result: EditChangeState) -> dict[str, object]:
    payload = health()
    payload["edit_change"] = result.to_dict()
    return payload


def _edit_navigation_payload(result: EditNavigationState) -> dict[str, object]:
    payload = health()
    payload["edit_navigation"] = result.to_dict()
    return payload


def _edit_history_payload(result: EditHistoryState) -> dict[str, object]:
    payload = health()
    payload["edit_history"] = result.to_dict()
    return payload


def _people_state_payload(state: PeopleState) -> dict[str, object]:
    payload = health()
    payload["people"] = state.to_dict()
    return payload


def _people_mutation_payload(result: PeopleMutation) -> dict[str, object]:
    payload = health()
    payload.update(result.to_dict())
    return payload


def _workflow_result_payload(result: WorkflowFacadeResult) -> dict[str, object]:
    payload = health()
    payload.update(result.to_dict())
    return payload


def _workflow_status_payload(status: dict[str, object]) -> dict[str, object]:
    payload = health()
    payload["status"] = status
    return payload


def _media_operation_status_payload(
    operation: MediaOperationRecord,
) -> dict[str, object]:
    payload = health()
    payload["media_operation"] = operation.to_dict()
    return payload


def _alignment_operation_payload(outcome: Any) -> dict[str, object]:
    payload = health()
    payload["media_operation"] = outcome.record.to_dict()
    payload["operation_readback"] = outcome.readback
    payload["alignment"] = None if outcome.artifact is None else outcome.artifact.to_dict()
    return payload


def _parallel_prepare_payload(outcome: Any) -> dict[str, object]:
    payload = health()
    payload["prepare_ref"] = outcome.prepare_ref
    payload["summary"] = outcome.summary
    return payload


def _parallel_operation_payload(outcome: Any) -> dict[str, object]:
    payload = health()
    payload["media_operation"] = outcome.record.to_dict()
    payload["operation_readback"] = outcome.readback
    payload["parallel_render"] = outcome.result
    return payload


def _validate_nle_export_cli_options(arguments: list[str]) -> None:
    allowed = {
        "approve-nle-export",
        "--project",
        "--run-id",
        "--action-id",
        "--edit-version-id",
        "--expected-revision",
        "--route",
        "--destination",
        "--alignment-artifact-id",
        "--json",
    }
    supplied = {
        argument.split("=", 1)[0]
        for argument in arguments
        if argument.startswith("--")
    }
    if not supplied <= allowed:
        _json_error("invalid_arguments")


def _validate_workflow_cli_options(arguments: list[str], command: str) -> None:
    allowed = {
        "workflow-start": {
            "--project",
            "--run-id",
            "--ordered-source-ids-json",
            "--json",
        },
        "workflow-status": {"--project", "--run-id", "--json"},
        "workflow-action": {
            "--project",
            "--run-id",
            "--action-id",
            "--action",
            "--input-json",
            "--json",
        },
        "workflow-cancel": {
            "--project",
            "--run-id",
            "--action-id",
            "--json",
        },
    }[command]
    supplied = {
        argument.split("=", 1)[0]
        for argument in arguments[1:]
        if argument.startswith("--")
    }
    if not supplied <= allowed:
        _json_error("invalid_arguments")


def _validate_media_start_cli_options(arguments: list[str], command: str) -> None:
    allowed = {
        "transcribe-source": {
            "--project",
            "--operation-id",
            "--source-id",
            "--expected-revision",
            "--speaker-diarization",
            "--json",
        },
        "proxy-create": {
            "--project",
            "--operation-id",
            "--source-id",
            "--expected-revision",
            "--json",
        },
        "align-multicam": {
            "--project", "--operation-id", "--alignment-id", "--expected-revision",
            "--main-camera-json", "--auxiliary-cameras-json", "--main-audio-stable",
            "--max-temporary-disk-bytes", "--max-analysis-memory-bytes",
            "--max-runtime-seconds", "--json",
        },
        "multicam-parallel-render-prepare": {
            "--project", "--edit-version-id", "--alignment-ref-json",
            "--auxiliary-camera-ids-json", "--expected-revision", "--json",
        },
        "multicam-parallel-render-start": {
            "--project", "--operation-id", "--prepare-ref-json", "--json",
        },
    }[command]
    supplied = {
        argument.split("=", 1)[0]
        for argument in arguments[1:]
        if argument.startswith("--")
    }
    if not supplied <= allowed:
        _json_error("invalid_arguments")


def _option_present(arguments: list[str], option: str) -> bool:
    return any(argument == option or argument.startswith(f"{option}=") for argument in arguments)


def _parse_clips_json(value: str) -> list[dict[str, object]]:
    data = json.loads(value)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("clips JSON must be an array of objects")
    return data


def _parse_tags_json(value: str) -> list[str]:
    data = json.loads(value)
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError("tags JSON must be an array of strings")
    return data


def _parse_string_array_json(value: str) -> list[str]:
    data = json.loads(value)
    if not isinstance(data, list) or any(
        not isinstance(item, str) for item in data
    ):
        raise ValueError("JSON value must be an array of strings")
    return data
    return data


def _parse_object_array_json(value: str) -> list[dict[str, object]]:
    data = json.loads(value)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("JSON value must be an array of objects")
    return data


def _parse_object_json(value: str) -> dict[str, object]:
    data = json.loads(value)
    if not isinstance(data, dict):
        raise _InvalidArgumentType("JSON must be an object")
    return data


if __name__ == "__main__":
    main(sys.argv[1:])
