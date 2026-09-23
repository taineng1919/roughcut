"""Loopback-only HTTP review server with project-scoped media access."""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast
from urllib.parse import parse_qs, unquote, urlsplit

from roughcut.adapters.artifact_store import read_json_object
from roughcut.adapters.project_lock import project_write_lock
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.content_drafts import read_content_draft
from roughcut.application.draft_editor import (
    DraftEditorCacheKey,
    DraftEditorSelection,
    DraftEditorSnapshot,
    DraftEditorTranscriptBase,
    load_draft_editor_snapshot_from_workflow,
    read_draft_transcript_window,
    resolve_draft_editor_caret,
    resolve_draft_editor_selection,
    search_draft_editor,
    validate_draft_editor_snapshot,
)
from roughcut.application.draft_workspaces import (
    DraftWorkspaceState,
    edit_draft_workspace,
    edit_draft_workspace_narration,
    edit_draft_workspace_punctuation,
    edit_draft_workspace_section,
    open_draft_workspace,
    read_draft_workspace,
    redo_draft_workspace,
    select_draft_workspace_candidate,
    undo_draft_workspace,
)
from roughcut.application.edits import (
    EditNavigationState,
    change_edit,
    propose_review_edit_change,
    read_edit_history,
    read_review_restorable_clips,
    redo_edit,
    undo_edit,
)
from roughcut.application.health import health
from roughcut.application.preview import ReviewSnapshot, load_review_snapshot
from roughcut.application.proposals import read_proposal_diff
from roughcut.application.proxies import resolve_frozen_proxy_path
from roughcut.application.readable_transcripts import (
    read_readable_transcript,
    resolve_transcript_selection,
)
from roughcut.application.transcription import read_transcript_page
from roughcut.application.transcripts import (
    activate_transcript_version,
    correct_transcript,
    read_transcript_versions,
)
from roughcut.application.workflow_review import (
    WorkflowReviewSnapshot,
    load_workflow_review_content_draft_child,
    load_workflow_review_snapshot,
    reopen_workflow_review_content_draft,
    select_workflow_review_content_draft,
    workflow_review_payload,
    workflow_session_status,
)
from roughcut.application.workflows import (
    WorkflowFacadeResult,
    workflow_action,
    workflow_review_content_draft_ref,
    workflow_status,
)
from roughcut.domain.content_draft import NarrationBlock
from roughcut.domain.draft_workspace import DraftWorkspaceCheckpointRef
from roughcut.domain.edit import EditClip, MultiSourceEditProposal
from roughcut.domain.errors import WorkflowError
from roughcut.domain.project import ImportMode, Project, ProjectError, SourceAsset
from roughcut.domain.readable_transcript import (
    canonical_text_for_range,
    codepoint_to_utf16_offset,
    exact_fine_unit_spans,
    trusted_fine_unit_spans,
    utf16_to_codepoint_offset,
)
from roughcut.domain.workflow import ArtifactRef, canonical_sha256_v1

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_DRAFT_WORKSPACE_OPERATION_ID = re.compile(
    r"^dwop_(0|[1-9][0-9]*)_[a-f0-9]{32}$"
)
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_MAX_JSON_BODY = 1_000_000


def _schema2_ref_identity(ref: object) -> dict[str, object]:
    value = ref if isinstance(ref, dict) else cast(Any, ref).to_dict()
    return {
        key: value[key]
        for key in (
            "source_id",
            "transcript_version_id",
            "segment_id",
            "start_ticks",
            "end_ticks",
        )
    }


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


@dataclass
class _ReviewState:
    snapshot: ReviewSnapshot | WorkflowReviewSnapshot
    review_session_id: str
    draft_editor_snapshot: DraftEditorSnapshot | None = None
    draft_editor_transcript_base: DraftEditorTranscriptBase | None = None
    draft_workspace: DraftWorkspaceState | None = None
    draft_workspace_error: WorkflowError | None = None
    draft_editor_lock: threading.RLock = field(default_factory=threading.RLock)
    workflow_content_draft_id: str | None = None
    workflow_proposal_id: str | None = None
    roughcut_undo: list[tuple[str, str]] = field(default_factory=list)
    roughcut_redo: list[tuple[str, str]] = field(default_factory=list)
    roughcut_restoration_clips: tuple[EditClip, ...] = ()
    roughcut_last_adopted_proposal_id: str | None = None
    workflow_action_requests: dict[
        str, tuple[str, dict[str, object]]
    ] = field(default_factory=dict)
    workflow_action_lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass(repr=True)
class RunningReviewServer:
    port: int
    url: str = field(repr=False)
    thread: threading.Thread
    token: str = field(repr=False)
    _server: ThreadingHTTPServer = field(repr=False)
    _closed: bool = field(default=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("review server did not stop")

    def wait(self) -> None:
        self.thread.join()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def start_review_server(
    project_path: Path,
    *,
    proposal_id: str | None = None,
    edit_version_id: str | None = None,
    source_bindings: list[dict[str, object]] | None = None,
    content_draft_id: str | None = None,
    static_root: Path | None = None,
    token_ttl_seconds: int = 3600,
) -> RunningReviewServer:
    if source_bindings is not None:
        if proposal_id is not None or edit_version_id is not None:
            raise ProjectError(
                "workflow bindings are mutually exclusive with Proposal or Decision review"
            )
        snapshot: ReviewSnapshot | WorkflowReviewSnapshot = (
            load_workflow_review_snapshot(
                project_path,
                source_bindings=source_bindings,
                content_draft_id=content_draft_id,
            )
        )
    else:
        if content_draft_id is not None:
            raise ProjectError("workflow content draft requires explicit source bindings")
        snapshot = load_review_snapshot(
            project_path, proposal_id=proposal_id, edit_version_id=edit_version_id
        )
    selected_static = static_root or Path(__file__).with_name("static")
    resolved_static = selected_static.resolve(strict=True)
    if not (resolved_static / "index.html").is_file():
        raise ProjectError("review static assets are missing")
    if token_ttl_seconds <= 0:
        raise ProjectError("review token lifetime must be positive")
    token = secrets.token_urlsafe(32)
    review_session_id = "review_session_" + secrets.token_hex(12)
    draft_workspace = None
    draft_workspace_error = None
    if isinstance(snapshot, WorkflowReviewSnapshot):
        try:
            status = workflow_status(snapshot.project_path)
        except WorkflowError as error:
            if error.code != "workflow_required":
                raise
            status = None
        run = status["workflow_run"] if status is not None else None
        if run is not None and (
            not isinstance(run, dict) or not isinstance(run.get("run_id"), str)
        ):
            raise WorkflowError(
                "workflow_integrity_error",
                "Roughcut workflow façade returned an invalid WorkflowRun to Review",
            )
        artifact_refs = run.get("artifact_refs") if isinstance(run, dict) else None
        draft_ref = (
            artifact_refs.get("content_draft")
            if isinstance(artifact_refs, dict)
            else None
        )
        if (
            isinstance(run, dict)
            and run.get("stage") == "draft_review"
            and draft_ref is not None
        ):
            try:
                draft_workspace = open_draft_workspace(
                    snapshot.project_path,
                    run_id=run["run_id"],
                    audit_review_session_id=review_session_id,
                )
            except WorkflowError as error:
                draft_workspace_error = error
            if (
                draft_workspace is not None
                and draft_workspace.current_candidate.confirmed_by_user
            ):
                snapshot = reopen_workflow_review_content_draft(
                    snapshot.project_path,
                    source_bindings=_workflow_bindings_payload(snapshot),
                    content_draft_id=(
                        draft_workspace.current_candidate.content_draft_id
                    ),
                    playback_selections=snapshot.playback_selections,
                )
            elif draft_workspace is not None:
                snapshot = load_workflow_review_snapshot(
                    snapshot.project_path,
                    source_bindings=_workflow_bindings_payload(snapshot),
                    content_draft_id=(
                        draft_workspace.current_candidate.content_draft_id
                    ),
                    playback_selections=snapshot.playback_selections,
                )
    draft_editor_snapshot = (
        load_draft_editor_snapshot_from_workflow(
            snapshot,
            content_draft_id=snapshot.content_draft.content_draft.content_draft_id,
        )
        if isinstance(snapshot, WorkflowReviewSnapshot)
        and snapshot.content_draft is not None
        and snapshot.content_draft.status == "current"
        else None
    )
    state = _ReviewState(
        snapshot,
        review_session_id,
        draft_editor_snapshot=draft_editor_snapshot,
        draft_editor_transcript_base=(
            draft_editor_snapshot.transcript_base
            if draft_editor_snapshot is not None
            else None
        ),
        draft_workspace=draft_workspace,
        draft_workspace_error=draft_workspace_error,
        roughcut_restoration_clips=(
            snapshot.proposal.clips if isinstance(snapshot, ReviewSnapshot) else ()
        ),
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _handler_factory(
            state=state,
            token=token,
            expires_at=time.monotonic() + token_ttl_seconds,
            static_root=resolved_static,
        ),
    )
    server.daemon_threads = True
    port = int(server.server_address[1])
    cast(Any, server).roughcut_port = port
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"roughcut-review-{port}",
        daemon=False,
    )
    try:
        thread.start()
        _probe_review_server(
            port,
            token,
            snapshot_path=(
                "/api/workflow/draft-editor"
                if draft_editor_snapshot is not None
                else (
                    "/api/workflow"
                    if isinstance(snapshot, WorkflowReviewSnapshot)
                    else "/api/review"
                )
            ),
        )
    except Exception:
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=5)
        server.server_close()
        raise
    return RunningReviewServer(
        port=port,
        url=f"http://127.0.0.1:{port}/?token={token}",
        thread=thread,
        token=token,
        _server=server,
    )


def _probe_review_server(port: int, token: str, *, snapshot_path: str) -> None:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        for path in ("/", snapshot_path):
            connection.request(
                "HEAD",
                path,
                headers={"X-Roughcut-Token": token},
            )
            response = connection.getresponse()
            response.read()
            if response.status != HTTPStatus.OK:
                raise ProjectError("review service readiness check failed")
    except OSError as error:
        raise ProjectError("review service readiness check failed") from error
    finally:
        connection.close()


def _handler_factory(
    *, state: _ReviewState, token: str, expires_at: float, static_root: Path
) -> type[BaseHTTPRequestHandler]:
    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "roughcut-review"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self._dispatch(head_only=False)

        def do_HEAD(self) -> None:
            self._dispatch(head_only=True)

        def do_POST(self) -> None:
            if not self._valid_host() or not self._valid_origin(required=True):
                self._json_error(HTTPStatus.FORBIDDEN, "forbidden", "request origin is not allowed")
                return
            if not self._authorized(allow_query=False):
                self._json_error(HTTPStatus.FORBIDDEN, "forbidden", "review session is invalid")
                return
            try:
                payload = self._read_json()
                if self.path == "/api/proposals":
                    self._create_proposal(payload)
                elif self.path == "/api/confirm":
                    self._confirm(payload)
                elif self.path == "/api/reject":
                    self._reject(payload)
                elif self.path == "/api/transcript-corrections":
                    self._correct_transcript(payload)
                elif self.path == "/api/transcript-activate":
                    self._activate_transcript(payload)
                elif self.path == "/api/edit-change":
                    self._change_edit(payload)
                elif self.path == "/api/edit-undo":
                    self._navigate_edit(payload, redo=False)
                elif self.path == "/api/edit-redo":
                    self._navigate_edit(payload, redo=True)
                elif self.path == "/api/roughcut-change":
                    self._change_roughcut_candidate(payload)
                elif self.path == "/api/roughcut-undo":
                    self._navigate_roughcut_candidate(payload, redo=False)
                elif self.path == "/api/roughcut-redo":
                    self._navigate_roughcut_candidate(payload, redo=True)
                elif self.path == "/api/roughcut-adopt":
                    self._adopt_roughcut_candidate(payload)
                elif self.path == "/api/roughcut-return-draft":
                    self._return_roughcut_to_draft(payload)
                elif self.path == "/api/workflow/brief":
                    self._create_workflow_brief(payload)
                elif self.path == "/api/workflow/readable-transcript":
                    self._read_workflow_transcript(payload)
                elif self.path == "/api/workflow/selection-resolve":
                    self._resolve_workflow_selection(payload)
                elif self.path == "/api/workflow/content-drafts":
                    self._create_workflow_content_draft(payload)
                elif self.path == "/api/workflow/content-draft-confirm":
                    self._confirm_workflow_content_draft(payload)
                elif self.path == "/api/workflow/content-draft-propose":
                    self._propose_workflow_content_draft(payload)
                elif self.path == "/api/workflow/draft-selection-resolve":
                    self._resolve_draft_editor_selection(payload)
                elif self.path == "/api/workflow/draft-caret-resolve":
                    self._resolve_draft_editor_caret(payload)
                elif self.path == "/api/workflow/draft-edit":
                    self._edit_draft_candidate(payload)
                elif self.path == "/api/workflow/draft-narration":
                    self._update_draft_narration(payload)
                elif self.path == "/api/workflow/draft-search":
                    self._search_draft_editor(payload)
                elif self.path == "/api/workflow/draft-transcript-window":
                    self._read_draft_transcript_window(payload)
                elif self.path == "/api/workflow/draft-candidate-select":
                    self._select_external_draft_candidate(payload)
                elif self.path == "/api/workflow/draft-agent-handoff":
                    self._draft_agent_handoff(payload)
                elif self.path == "/api/workflow/draft-undo":
                    self._navigate_draft_candidate(payload, redo=False)
                elif self.path == "/api/workflow/draft-redo":
                    self._navigate_draft_candidate(payload, redo=True)
                elif self.path == "/api/proposal-coverage":
                    self._read_proposal_coverage(payload)
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            except WorkflowError as error:
                if error.code == "draft_workspace_write_failed":
                    status = HTTPStatus.INTERNAL_SERVER_ERROR
                elif error.code in {
                    "workflow_action_conflict",
                    "workflow_recovery_conflict",
                    "workflow_stale",
                    "workflow_transition_not_allowed",
                    "draft_workspace_action_conflict",
                    "draft_workspace_stale",
                    "draft_workspace_transition_not_allowed",
                }:
                    status = HTTPStatus.CONFLICT
                else:
                    status = HTTPStatus.BAD_REQUEST
                self._json_error(status, error.code, str(error))
            except ProjectError as error:
                current_revision = ProjectStore(state.snapshot.project_path).load().revision
                write_endpoint = self.path in {
                    "/api/proposals",
                    "/api/confirm",
                    "/api/reject",
                    "/api/edit-change",
                    "/api/edit-undo",
                    "/api/edit-redo",
                    "/api/roughcut-change",
                    "/api/roughcut-undo",
                    "/api/roughcut-redo",
                    "/api/roughcut-adopt",
                    "/api/roughcut-return-draft",
                } or self.path.startswith("/api/workflow/")
                if (
                    str(error) == "project revision conflict"
                    or (str(error) == "edit base is stale")
                    or (
                        write_endpoint
                        and (
                            current_revision != state.snapshot.project_revision
                            or _snapshot_bindings_stale(state.snapshot)
                            or _snapshot_active_edit_stale(state.snapshot)
                        )
                    )
                ):
                    self._json_error(
                        HTTPStatus.CONFLICT,
                        "stale_review",
                        str(error),
                        current_revision=current_revision,
                    )
                else:
                    error_code = (
                        "invalid_workflow_change"
                        if self.path.startswith("/api/workflow/")
                        else "invalid_review_change"
                    )
                    self._json_error(HTTPStatus.BAD_REQUEST, error_code, str(error))
            except (TypeError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_json", "invalid JSON body")
            except OSError:
                try:
                    workflow_status(state.snapshot.project_path)
                except (OSError, ProjectError, WorkflowError):
                    pass
                self._json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "review_service_failed",
                    "review operation failed",
                )

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._method_not_allowed()

        def do_DELETE(self) -> None:
            self._method_not_allowed()

        def _method_not_allowed(self) -> None:
            if not self._valid_host() or not self._valid_origin(required=True):
                self._json_error(
                    HTTPStatus.FORBIDDEN,
                    "forbidden",
                    "request origin is not allowed",
                )
                return
            if not self._authorized(allow_query=False):
                self._json_error(
                    HTTPStatus.FORBIDDEN,
                    "forbidden",
                    "review session is invalid",
                )
                return
            self._json_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method_not_allowed",
                "request method is not allowed",
            )

        def _dispatch(self, *, head_only: bool) -> None:
            if not self._valid_host() or not self._valid_origin(required=False):
                self._json_error(HTTPStatus.FORBIDDEN, "forbidden", "request host is not allowed")
                return
            parsed = urlsplit(self.path)
            is_index = parsed.path in {"", "/"}
            if not self._authorized(allow_query=is_index):
                self._json_error(HTTPStatus.FORBIDDEN, "forbidden", "review session is invalid")
                return
            if parsed.path == "/api/review":
                try:
                    payload = (
                        workflow_review_payload(state.snapshot)
                        if isinstance(state.snapshot, WorkflowReviewSnapshot)
                        else state.snapshot.to_dict()
                    )
                except OSError:
                    self._json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "review_service_failed",
                        "review operation failed",
                    )
                    return
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
                return
            if parsed.path == "/api/workflow":
                if not isinstance(state.snapshot, WorkflowReviewSnapshot):
                    self._json_error(
                        HTTPStatus.CONFLICT,
                        "workflow_not_active",
                        "review is no longer in workflow mode",
                    )
                    return
                try:
                    payload = workflow_review_payload(state.snapshot)
                except OSError:
                    self._json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "review_service_failed",
                        "review operation failed",
                    )
                    return
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
                return
            if parsed.path == "/api/workflow/brief":
                try:
                    workflow = self._require_workflow_review()
                except ProjectError as error:
                    self._json_error(
                        HTTPStatus.CONFLICT,
                        "workflow_not_active",
                        str(error),
                    )
                    return
                except OSError:
                    self._json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "review_service_failed",
                        "review operation failed",
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "brief": (
                            workflow.brief.to_dict()
                            if workflow.brief is not None
                            else None
                        ),
                        "project_revision": workflow.project_revision,
                        "session": workflow_session_status(workflow).to_dict(),
                    },
                    head_only=head_only,
                )
                return
            if parsed.path == "/api/workflow/content-draft":
                try:
                    workflow = self._require_workflow_review()
                    if workflow.content_draft is None:
                        raise ProjectError("workflow has no selected Content Draft")
                    draft = read_content_draft(
                        workflow.project_path,
                        workflow.content_draft.content_draft.content_draft_id,
                    )
                except ProjectError as error:
                    self._json_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_workflow_change",
                        str(error),
                    )
                    return
                except OSError:
                    self._json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "review_service_failed",
                        "review operation failed",
                    )
                    return
                self._send_json(HTTPStatus.OK, draft.to_dict(), head_only=head_only)
                return
            if parsed.path == "/api/workflow/draft-editor":
                try:
                    editor = self._draft_editor_snapshot()
                    payload = self._draft_editor_payload(editor)
                except WorkflowError as error:
                    self._json_error(
                        HTTPStatus.CONFLICT,
                        error.code,
                        str(error),
                    )
                    return
                except ProjectError as error:
                    self._json_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_workflow_change",
                        str(error),
                    )
                    return
                except OSError:
                    self._json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "review_service_failed",
                        "review operation failed",
                    )
                    return
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
                return
            if parsed.path == "/api/proposal-diff":
                self._read_proposal_diff(head_only=head_only)
                return
            if parsed.path == "/api/transcript-versions":
                try:
                    artifact_review = self._require_artifact_review()
                    payload = _transcript_review_payload(artifact_review)
                except ProjectError as error:
                    self._json_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_transcript_versions",
                        str(error),
                    )
                    return
                except OSError as error:
                    self._json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "review_service_failed",
                        str(error),
                    )
                    return
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
                return
            if parsed.path == "/api/roughcut-state":
                try:
                    self._require_writable_review()
                    payload = self._roughcut_state_payload()
                except ProjectError as error:
                    current_revision = ProjectStore(state.snapshot.project_path).load().revision
                    if (
                        current_revision != state.snapshot.project_revision
                        or _snapshot_bindings_stale(state.snapshot)
                        or _snapshot_active_edit_stale(state.snapshot)
                    ):
                        self._json_error(
                            HTTPStatus.CONFLICT,
                            "stale_review",
                            str(error),
                            current_revision=current_revision,
                        )
                    else:
                        self._json_error(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_review_change",
                            str(error),
                        )
                    return
                except OSError:
                    self._json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "review_service_failed",
                        "review operation failed",
                    )
                    return
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
                return
            if parsed.path == "/api/edit-history":
                try:
                    decision_review = self._require_decision_review()
                    self._require_writable_review()
                    payload = _review_edit_payload(decision_review)
                except ProjectError as error:
                    current_revision = ProjectStore(state.snapshot.project_path).load().revision
                    if (
                        current_revision != state.snapshot.project_revision
                        or _snapshot_bindings_stale(state.snapshot)
                        or _snapshot_active_edit_stale(state.snapshot)
                    ):
                        self._json_error(
                            HTTPStatus.CONFLICT,
                            "stale_review",
                            str(error),
                            current_revision=current_revision,
                        )
                    else:
                        self._json_error(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_review_change",
                            str(error),
                        )
                    return
                except OSError as error:
                    self._json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "review_service_failed",
                        str(error),
                    )
                    return
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
                return
            if parsed.path == "/favicon.ico":
                self._send_bytes(
                    HTTPStatus.NO_CONTENT,
                    b"",
                    headers={},
                    head_only=head_only,
                )
                return
            if parsed.path.startswith("/media/"):
                self._serve_media(parsed.path.removeprefix("/media/"), head_only=head_only)
                return
            self._serve_static(parsed.path, set_cookie=is_index, head_only=head_only)

        def _authorized(self, *, allow_query: bool) -> bool:
            if time.monotonic() >= expires_at:
                return False
            provided = self.headers.get("X-Roughcut-Token")
            if provided is None and allow_query:
                values = parse_qs(urlsplit(self.path).query).get("token", [])
                provided = values[0] if len(values) == 1 else None
            if provided is None:
                cookies = self.headers.get("Cookie", "")
                for cookie in cookies.split(";"):
                    name, separator, value = cookie.strip().partition("=")
                    if separator and name == "roughcut_session":
                        provided = value
                        break
            return provided is not None and hmac.compare_digest(provided, token)

        def _valid_host(self) -> bool:
            port = int(getattr(self.server, "roughcut_port", 0))
            return self.headers.get("Host", "") in {
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            }

        def _valid_origin(self, *, required: bool) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return not required
            port = int(getattr(self.server, "roughcut_port", 0))
            return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

        def _serve_static(self, request_path: str, *, set_cookie: bool, head_only: bool) -> None:
            relative = (
                "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
            )
            candidate = (static_root / relative).resolve()
            if not candidate.is_relative_to(static_root) or not candidate.is_file():
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "static asset not found")
                return
            body = candidate.read_bytes()
            headers = {
                "Content-Type": mimetypes.guess_type(candidate.name)[0]
                or "application/octet-stream"
            }
            if set_cookie:
                headers["Set-Cookie"] = (
                    f"roughcut_session={token}; HttpOnly; SameSite=Strict; Path=/"
                )
            self._send_bytes(HTTPStatus.OK, body, headers=headers, head_only=head_only)

        def _serve_media(self, source_id: str, *, head_only: bool) -> None:
            if _SAFE_ID.fullmatch(source_id) is None:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "source not found")
                return
            if source_id not in state.snapshot.authorized_source_ids:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "source not found")
                return
            project = ProjectStore(state.snapshot.project_path).load()
            source = next((item for item in project.sources if item.source_id == source_id), None)
            if source is None:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "source not found")
                return
            playback = state.snapshot.playback_for(source_id)
            try:
                if playback.proxy is not None:
                    media_path = resolve_frozen_proxy_path(
                        state.snapshot.project_path,
                        playback.proxy,
                    )
                    content_type = "video/mp4"
                else:
                    media_path = _registered_media_path(
                        state.snapshot.project_path,
                        source,
                    )
                    content_type = (
                        mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
                    )
                size = media_path.stat().st_size
            except (OSError, ProjectError):
                if playback.playback_kind == "proxy":
                    self._json_error(
                        HTTPStatus.CONFLICT,
                        "proxy_unavailable",
                        "selected proxy is unavailable; recreate it or restart Review",
                    )
                    return
                self._json_error(
                    HTTPStatus.FORBIDDEN,
                    "forbidden_media",
                    "registered media is unavailable",
                )
                return
            range_header = self.headers.get("Range")
            if range_header is None:
                start, end, status = 0, size - 1, HTTPStatus.OK
            else:
                try:
                    start, end = _parse_range(range_header, size)
                except ProjectError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": content_type,
            }
            if status == HTTPStatus.PARTIAL_CONTENT:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            self.send_response(status)
            self._security_headers()
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if not head_only:
                with media_path.open("rb") as media_file:
                    media_file.seek(start)
                    remaining = length
                    while remaining:
                        chunk = media_file.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        remaining -= len(chunk)

        def _create_proposal(self, payload: dict[str, Any]) -> None:
            del payload
            raise WorkflowError(
                "workflow_transition_not_allowed",
                "Roughcut workflow façade rejected direct Review Proposal creation; "
                "confirm the current Draft through approve_draft",
            )

        def _create_workflow_brief(self, payload: dict[str, Any]) -> None:
            workflow = self._require_writable_workflow()
            _validate_payload_keys(
                payload,
                required={"theme", "target_duration_ticks", "focus", "allow_reorder"},
            )
            theme = payload["theme"]
            target = payload["target_duration_ticks"]
            focus = payload["focus"]
            allow_reorder = payload["allow_reorder"]
            if not isinstance(theme, str):
                raise ProjectError("workflow Brief theme must be a string")
            if isinstance(target, bool) or not isinstance(target, int):
                raise ProjectError("workflow Brief target duration must be an integer")
            if not isinstance(focus, list) or not all(isinstance(item, str) for item in focus):
                raise ProjectError("workflow Brief focus must be a string array")
            if not isinstance(allow_reorder, bool):
                raise ProjectError("workflow Brief reorder flag must be a boolean")
            with state.workflow_action_lock:
                request_key = self._workflow_request_key("confirm_brief", payload)
                if request_key in state.workflow_action_requests:
                    result = self._repeat_workflow_action(
                        workflow.project_path,
                        "confirm_brief",
                        payload,
                    )
                else:
                    status = self._finite_workflow_status(workflow.project_path)
                    basis = status["confirmation_bases"]["brief"]
                    if not isinstance(basis, dict) or not isinstance(
                        basis.get("basis"), dict
                    ):
                        raise WorkflowError(
                            "workflow_transition_not_allowed",
                            "Roughcut workflow façade did not offer Brief confirmation "
                            "for the current Review state",
                        )
                    result = self._perform_workflow_action(
                        workflow.project_path,
                        "confirm_brief",
                        {
                            "schema_version": 1,
                            "confirmation_basis": basis["basis"],
                            "theme": theme,
                            "target_duration_ticks": target,
                            "focus": focus,
                            "allow_reorder": allow_reorder,
                            "speaker_resolution_waivers": [],
                        },
                        request_payload=payload,
                    )
            if result.receipt is None:
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut workflow façade omitted the confirm_brief receipt",
                )
            selected_draft_id = (
                workflow.content_draft.content_draft.content_draft_id
                if workflow.content_draft is not None
                else None
            )
            state.snapshot = load_workflow_review_snapshot(
                workflow.project_path,
                source_bindings=_workflow_bindings_payload(workflow),
                content_draft_id=selected_draft_id,
                playback_selections=workflow.playback_selections,
            )
            assert isinstance(state.snapshot, WorkflowReviewSnapshot)
            state.draft_editor_snapshot = None
            state.draft_editor_transcript_base = None
            state.draft_workspace = None
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "brief_mutation": {
                        "brief": self._read_workflow_artifact(
                            workflow.project_path,
                            result,
                            "brief",
                        ),
                        "project_revision": result.receipt.after.project_revision,
                    },
                    "workflow_receipt": result.receipt.to_dict(),
                    "workflow": workflow_review_payload(state.snapshot),
                },
            )

        def _read_workflow_transcript(self, payload: dict[str, Any]) -> None:
            workflow = self._require_writable_workflow()
            _validate_payload_keys(
                payload,
                required={"offset", "limit"},
                optional={"filters", "overlay"},
            )
            offset = payload["offset"]
            limit = payload["limit"]
            filters = payload.get("filters")
            overlay = payload.get("overlay")
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise ProjectError("workflow transcript offset must be an integer")
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ProjectError("workflow transcript limit must be an integer")
            if filters is not None and not isinstance(filters, dict):
                raise ProjectError("workflow transcript filters must be an object")
            if overlay is not None and not isinstance(overlay, dict):
                raise ProjectError("workflow transcript overlay must be an object")
            page = read_readable_transcript(
                workflow.project_path,
                source_bindings=_workflow_bindings_payload(workflow),
                expected_revision=workflow.project_revision,
                offset=offset,
                limit=limit,
                filters=filters,
                overlay=overlay,
            )
            self._send_json(HTTPStatus.OK, page.to_dict())

        def _resolve_workflow_selection(self, payload: dict[str, Any]) -> None:
            workflow = self._require_writable_workflow()
            _validate_payload_keys(
                payload,
                required={"view_hash", "selections"},
                optional={"overlay"},
            )
            view_hash = payload["view_hash"]
            selections = payload["selections"]
            overlay = payload.get("overlay")
            if not isinstance(view_hash, str):
                raise ProjectError("workflow selection view hash must be a string")
            if not isinstance(selections, list) or not all(
                isinstance(item, dict) for item in selections
            ):
                raise ProjectError("workflow selections must be an object array")
            if overlay is not None and not isinstance(overlay, dict):
                raise ProjectError("workflow selection overlay must be an object")
            resolved = resolve_transcript_selection(
                workflow.project_path,
                source_bindings=_workflow_bindings_payload(workflow),
                expected_revision=workflow.project_revision,
                view_hash=view_hash,
                selections=selections,
                overlay=overlay,
            )
            self._send_json(HTTPStatus.OK, resolved.to_dict())

        def _create_workflow_content_draft(self, payload: dict[str, Any]) -> None:
            workflow = self._require_writable_workflow()
            _validate_payload_keys(
                payload,
                required={"blocks"},
                optional={
                    "parent_draft_id",
                    "display_title",
                    "scoped_mutable_block_ids",
                },
            )
            if workflow.brief is None or workflow.context_hash is None:
                raise ProjectError("workflow requires a current Brief before Content Draft")
            blocks = payload["blocks"]
            parent_id = payload.get("parent_draft_id")
            display_title = payload.get("display_title")
            if not isinstance(blocks, list) or not all(isinstance(item, dict) for item in blocks):
                raise ProjectError("workflow Content Draft blocks must be an object array")
            if parent_id is not None and not isinstance(parent_id, str):
                raise ProjectError("workflow Content Draft parent must be a string")
            if display_title is not None and not isinstance(display_title, str):
                raise ProjectError("workflow Content Draft display title must be a string")
            mutable = payload.get("scoped_mutable_block_ids", [])
            if not isinstance(mutable, list) or not all(
                isinstance(block_id, str) for block_id in mutable
            ):
                raise ProjectError("workflow mutable block IDs must be a string array")
            with project_write_lock(workflow.project_path):
                status = self._finite_workflow_status(workflow.project_path)
                run = status["workflow_run"]
                exact_bindings = [
                    {
                        "source_id": binding["source_id"],
                        "transcript_version_id": binding["transcript_version_id"],
                    }
                    for binding in run["ordered_bindings"]
                ]
                if exact_bindings != _workflow_bindings_payload(workflow):
                    raise WorkflowError(
                        "workflow_subject_mismatch",
                        "Roughcut workflow façade rejected Review bindings that do "
                        "not equal the active WorkflowRun",
                    )
                brief_ref = run["artifact_refs"]["brief"]
                if brief_ref is None:
                    raise WorkflowError(
                        "workflow_not_ready",
                        "Roughcut workflow façade requires a current Brief",
                    )
                if run["artifact_refs"]["content_draft"] is not None:
                    workspace = self._require_draft_workspace()
                    current_id = workspace.current_candidate.content_draft_id
                    if parent_id is None:
                        parent_id = current_id
                    elif parent_id != current_id:
                        raise WorkflowError(
                            "workflow_subject_mismatch",
                            "Review parent_draft_id must equal the persistent current "
                            "Draft workspace candidate",
                        )
                parent_ref = (
                    None
                    if parent_id is None
                    else workflow_review_content_draft_ref(
                        workflow.project_path,
                        run["run_id"],
                        parent_id,
                    )
                )
                result = self._perform_workflow_action(
                    workflow.project_path,
                    "submit_draft",
                    {
                        "schema_version": 1,
                        "parent_draft_ref": parent_ref,
                        "display_title": display_title,
                        "source_bindings": exact_bindings,
                        "brief_ref": brief_ref,
                        "context_hash": workflow.context_hash,
                        "blocks": [
                            self._closed_workflow_block(block) for block in blocks
                        ],
                        "scoped_mutable_block_ids": mutable,
                    },
                )
                if result.receipt is None:
                    raise WorkflowError(
                        "workflow_integrity_error",
                        "Roughcut workflow façade omitted the submit_draft receipt",
                    )
                mutation = result.receipt.mutation
                if mutation is None or mutation.kind != "content_draft":
                    raise WorkflowError(
                        "workflow_integrity_error",
                        "Roughcut workflow façade returned an invalid submit_draft receipt",
                    )
                workspace = open_draft_workspace(
                    workflow.project_path,
                    run_id=run["run_id"],
                    audit_review_session_id=state.review_session_id,
                )
                refreshed = self._install_draft_workspace(workflow, workspace)
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "content_draft_mutation": {
                            "content_draft": read_content_draft(
                                workflow.project_path, mutation.artifact_id
                            ).content_draft.to_dict(),
                            "project_revision": result.receipt.after.project_revision,
                        },
                        "workflow_receipt": result.receipt.to_dict(),
                        "workflow": workflow_review_payload(refreshed),
                    },
                )

        def _confirm_workflow_content_draft(self, payload: dict[str, Any]) -> None:
            _validate_payload_keys(
                payload,
                required={
                    "operation_id",
                    "expected_checkpoint_ref",
                    "expected_current_candidate_ref",
                    "content_draft_id",
                },
            )
            requested_id = payload["content_draft_id"]
            if not isinstance(requested_id, str):
                raise ProjectError("workflow Content Draft ID must be a string")
            if isinstance(state.snapshot, ReviewSnapshot):
                result = self._repeat_workflow_action(
                    state.snapshot.project_path,
                    "approve_draft",
                    payload,
                )
                self._send_confirmed_draft_result(result, requested_id)
                return
            workflow = self._require_writable_workflow()
            _, checkpoint_ref, current_ref = self._draft_workspace_envelope(payload)
            workspace = self._require_draft_workspace()
            if (
                workspace.checkpoint_ref != checkpoint_ref
                or workspace.checkpoint.current_candidate_ref != current_ref
                or requested_id != current_ref.artifact_id
            ):
                raise WorkflowError(
                    "draft_workspace_stale",
                    "Roughcut Draft workspace refused to approve a candidate other "
                    "than the exact checkpoint current",
                )
            content_draft_id = self._displayed_content_draft_id(workflow, payload)
            result = self._perform_workflow_action(
                workflow,
                "approve_draft",
                {
                    "schema_version": 1,
                    "content_draft_ref": current_ref.to_dict(),
                },
                request_payload=payload,
            )
            self._send_confirmed_draft_result(result, content_draft_id)

        def _propose_workflow_content_draft(self, payload: dict[str, Any]) -> None:
            _validate_payload_keys(payload, required={"content_draft_id"})
            requested_id = payload["content_draft_id"]
            if not isinstance(requested_id, str):
                raise ProjectError("workflow Content Draft ID must be a string")
            if isinstance(state.snapshot, ReviewSnapshot):
                if (
                    state.snapshot.basis_type != "proposal"
                    or state.workflow_content_draft_id != requested_id
                    or state.workflow_proposal_id != state.snapshot.basis_id
                ):
                    raise ProjectError(
                        "workflow roughcut preview retry does not match the generated Proposal"
                    )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "content_draft_proposal": {
                            "content_draft_id": requested_id,
                            "proposal_schema_version": state.snapshot.schema_version,
                            "proposal": state.snapshot.proposal.to_dict(),
                            "project_revision": state.snapshot.project_revision,
                        },
                        "review": state.snapshot.to_dict(),
                    },
                )
                return
            workflow = self._require_writable_workflow()
            self._displayed_content_draft_id(workflow, payload)
            raise WorkflowError(
                "workflow_transition_not_allowed",
                "Roughcut workflow façade requires approve_draft to confirm the "
                "Draft and generate its Proposal atomically",
            )

        def _resolve_draft_editor_selection(self, payload: dict[str, Any]) -> None:
            _validate_payload_keys(
                payload,
                required={"candidate_id", "surface", "anchor", "focus"},
            )
            editor = self._draft_editor_snapshot()
            self._require_displayed_candidate(editor, payload)
            surface = payload["surface"]
            anchor = payload["anchor"]
            focus = payload["focus"]
            if not isinstance(surface, str):
                raise ProjectError("draft editor selection surface must be a string")
            if not isinstance(anchor, dict) or not isinstance(focus, dict):
                raise ProjectError("draft editor selection endpoints must be objects")
            selection = resolve_draft_editor_selection(
                editor,
                surface=surface,
                anchor=anchor,
                focus=focus,
            )
            result = selection.to_dict()
            if surface == "draft":
                workspace = self._cached_draft_workspace()
                current_ref = workspace.checkpoint.current_candidate_ref
                display_payload = {
                    "anchor": self._schema2_selection_display_point(
                        editor, result["display_range"]["anchor"]  # type: ignore[index]
                    ),
                    "focus": self._schema2_selection_display_point(
                        editor, result["display_range"]["focus"]  # type: ignore[index]
                    ),
                }
                if selection.narration_block_id is not None:
                    source_payload: dict[str, object] = {
                        "kind": "resolved_selection",
                        "surface": "draft",
                        "selection_kind": "narration",
                        "display_range": display_payload,
                        "block_id": selection.narration_block_id,
                        "text": selection.narration_text,
                        "status": selection.narration_status,
                        "recorded_refs": [
                            _schema2_ref_identity(ref)
                            for ref in selection.narration_recorded_refs
                        ],
                    }
                else:
                    if selection.resolution is None:
                        raise ProjectError("draft editor selection resolution is missing")
                    source_payload = {
                        "kind": "resolved_selection",
                        "surface": "draft",
                        "selection_kind": "source_excerpt",
                        "display_range": display_payload,
                        "block_ids": self._draft_selection_block_ids(editor, selection),
                        "refs": [
                            _schema2_ref_identity(ref)
                            for ref in selection.resolution.refs
                        ],
                        "canonical_text": selection.resolution.canonical_text,
                        "degraded": selection.resolution.degraded,
                    }
                result["resolution_hash"] = canonical_sha256_v1(
                    {**source_payload, "candidate_ref": current_ref.to_dict()}
                )
            self._send_json(HTTPStatus.OK, result)

        def _schema2_selection_display_point(
            self, editor: DraftEditorSnapshot, value: object
        ) -> dict[str, object]:
            if not isinstance(value, dict):
                raise ProjectError("draft editor selection display point is invalid")
            paragraph_id = value.get("paragraph_id")
            utf16_offset = value.get("utf16_offset")
            if (
                not isinstance(paragraph_id, str)
                or isinstance(utf16_offset, bool)
                or not isinstance(utf16_offset, int)
            ):
                raise ProjectError("draft editor selection display point is invalid")
            paragraph = next(
                (item for item in editor.paragraphs if item["paragraph_id"] == paragraph_id),
                None,
            )
            if paragraph is None:
                raise ProjectError("draft editor selection paragraph is invalid")
            text = str(paragraph["text"])
            character_offset = utf16_to_codepoint_offset(text, utf16_offset)
            block_id = paragraph.get("block_id")
            if paragraph.get("kind") == "source_excerpt":
                block_id = self._source_display_block_id(paragraph, character_offset)
            if not isinstance(block_id, str):
                raise ProjectError("draft editor selection block identity is invalid")
            return {
                "paragraph_id": paragraph_id,
                "block_id": block_id,
                "utf16_offset": utf16_offset,
            }

        def _source_display_block_id(
            self, paragraph: dict[str, object], character_offset: int
        ) -> str | None:
            runs = paragraph.get("source_runs")
            if not isinstance(runs, list):
                raise ProjectError("draft editor source runs are invalid")
            for index, run in enumerate(runs):
                if not isinstance(run, dict):
                    continue
                block_id = run.get("block_id")
                start = run.get("start_offset")
                end = run.get("end_offset")
                if (
                    not isinstance(block_id, str)
                    or isinstance(start, bool)
                    or not isinstance(start, int)
                    or isinstance(end, bool)
                    or not isinstance(end, int)
                ):
                    continue
                if start <= character_offset < end:
                    return block_id
                if character_offset == end:
                    next_run = runs[index + 1] if index + 1 < len(runs) else None
                    next_start = next_run.get("start_offset") if isinstance(next_run, dict) else None
                    if next_start != character_offset:
                        return block_id
            return None

        def _draft_selection_block_ids(
            self, editor: DraftEditorSnapshot, selection: object
        ) -> list[str]:
            item_start = getattr(selection, "item_start", None)
            item_end = getattr(selection, "item_end", None)
            if not isinstance(item_start, int) or not isinstance(item_end, int):
                raise ProjectError("draft editor selection block range is missing")
            block_ids: list[str] = []
            for item in editor._items[item_start:item_end]:
                block_id = getattr(item, "block_id", None)
                if isinstance(block_id, str) and (not block_ids or block_ids[-1] != block_id):
                    block_ids.append(block_id)
            return block_ids

        def _resolve_draft_editor_caret(self, payload: dict[str, Any]) -> None:
            _validate_payload_keys(
                payload,
                required={"candidate_id", "paragraph_id", "offset"},
                optional={"offset_encoding"},
            )
            editor = self._draft_editor_snapshot()
            self._require_displayed_candidate(editor, payload)
            paragraph_id = payload["paragraph_id"]
            offset = payload["offset"]
            encoding = payload.get("offset_encoding", "codepoint")
            if not isinstance(paragraph_id, str):
                raise ProjectError("draft editor caret paragraph must be a string")
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise ProjectError("draft editor caret offset must be an integer")
            if not isinstance(encoding, str):
                raise ProjectError("draft editor caret encoding must be a string")
            caret = resolve_draft_editor_caret(
                editor,
                paragraph_id=paragraph_id,
                offset=offset,
                offset_encoding=encoding,
            )
            self._send_json(HTTPStatus.OK, caret.to_dict())

        def _edit_draft_candidate(self, payload: dict[str, Any]) -> None:
            if payload.get("schema_version") == 2:
                self._edit_draft_schema2(payload, request_started_at=time.perf_counter())
                return
            request_started_at = time.perf_counter()
            _validate_payload_keys(
                payload,
                required={
                    "operation_id",
                    "expected_checkpoint_ref",
                    "expected_current_candidate_ref",
                    "candidate_id",
                    "operation",
                    "selection",
                    "accept_degraded",
                },
                optional={"caret"},
            )
            revalidation_started_at = time.perf_counter()
            operation_id, checkpoint_ref, current_ref = (
                self._draft_workspace_envelope(payload)
            )
            editor = self._draft_editor_snapshot_for_ref(current_ref)
            self._require_displayed_candidate(editor, payload)
            operation = payload["operation"]
            selection_payload = payload["selection"]
            accept_degraded = payload["accept_degraded"]
            caret_payload = payload.get("caret")
            if not isinstance(operation, str):
                raise ProjectError("draft editor operation must be a string")
            if not isinstance(selection_payload, dict):
                raise ProjectError("draft editor selection must be an object")
            if not isinstance(accept_degraded, bool):
                raise ProjectError("draft editor degraded acceptance must be boolean")
            _validate_payload_keys(
                selection_payload,
                required={"surface", "anchor", "focus"},
            )
            surface = selection_payload["surface"]
            anchor = selection_payload["anchor"]
            focus = selection_payload["focus"]
            if (
                not isinstance(surface, str)
                or not isinstance(anchor, dict)
                or not isinstance(focus, dict)
            ):
                raise ProjectError("draft editor selection fields are invalid")
            selection = resolve_draft_editor_selection(
                editor,
                surface=surface,
                anchor=anchor,
                focus=focus,
            )
            caret = None
            if caret_payload is not None:
                if not isinstance(caret_payload, dict):
                    raise ProjectError("draft editor caret must be an object")
                _validate_payload_keys(
                    caret_payload,
                    required={"paragraph_id", "offset"},
                    optional={"offset_encoding"},
                )
                paragraph_id = caret_payload["paragraph_id"]
                offset = caret_payload["offset"]
                encoding = caret_payload.get("offset_encoding", "codepoint")
                if (
                    not isinstance(paragraph_id, str)
                    or isinstance(offset, bool)
                    or not isinstance(offset, int)
                    or not isinstance(encoding, str)
                ):
                    raise ProjectError("draft editor caret fields are invalid")
                caret = resolve_draft_editor_caret(
                    editor,
                    paragraph_id=paragraph_id,
                    offset=offset,
                    offset_encoding=encoding,
                )
            revalidation_ms = _elapsed_ms(revalidation_started_at)
            child_write_started_at = time.perf_counter()
            workspace = edit_draft_workspace(
                editor.workflow.project_path,
                run_id=self._cached_draft_workspace().checkpoint.workflow_run_id,
                operation_id=operation_id,
                expected_checkpoint_ref=checkpoint_ref,
                expected_current_candidate_ref=current_ref,
                operation=operation,
                selection=selection,
                caret=caret,
                accept_degraded=accept_degraded,
                audit_review_session_id=state.review_session_id,
                prepared_editor_snapshot=editor,
            )
            child_write_ms = _elapsed_ms(child_write_started_at)
            refresh_started_at = time.perf_counter()
            self._install_draft_workspace(editor.workflow, workspace)
            workflow_refresh_ms = _elapsed_ms(refresh_started_at)
            validation_ms = 0.0
            rebuild_started_at = time.perf_counter()
            refreshed = self._draft_editor_snapshot()
            draft_rebuild_ms = _elapsed_ms(rebuild_started_at)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "operation": operation,
                    "selection": selection.to_dict(),
                    "draft_editor": self._draft_editor_payload(refreshed),
                    "timing": {
                        "selection_caret_revalidation_ms": revalidation_ms,
                        "immutable_child_write_fsync_ms": child_write_ms,
                        "project_brief_transcript_context_validation_ms": validation_ms,
                        "workflow_snapshot_refresh_ms": workflow_refresh_ms,
                        "draft_snapshot_rebuild_ms": draft_rebuild_ms,
                        "server_before_response_ms": _elapsed_ms(request_started_at),
                    },
                },
            )

        def _edit_draft_schema2(
            self,
            payload: dict[str, Any],
            *,
            request_started_at: float,
        ) -> None:
            with project_write_lock(state.snapshot.project_path):
                self._edit_draft_schema2_locked(
                    payload,
                    request_started_at=request_started_at,
                )

        def _edit_draft_schema2_locked(
            self,
            payload: dict[str, Any],
            *,
            request_started_at: float,
        ) -> None:
            revalidation_started_at = time.perf_counter()
            operation = payload.get("operation")
            if operation == "punctuation_edit":
                _validate_payload_keys(
                    payload,
                    required={
                        "schema_version",
                        "operation_id",
                        "expected_checkpoint_ref",
                        "expected_current_candidate_ref",
                        "operation",
                        "payload",
                    },
                )
                if payload["schema_version"] != 2 or not isinstance(payload["payload"], dict):
                    raise ProjectError("schema 2 punctuation envelope is invalid")
                operation_id, checkpoint_ref, current_ref = self._draft_workspace_envelope(payload)
                editor = self._draft_editor_snapshot_for_ref(current_ref)
                punctuation_payload = payload["payload"]
                _validate_payload_keys(
                    punctuation_payload,
                    required={
                        "paragraph_id",
                        "block_id",
                        "start_utf16_offset",
                        "end_utf16_offset",
                        "replacement",
                    },
                )
                paragraph_id = punctuation_payload["paragraph_id"]
                block_id = punctuation_payload["block_id"]
                start_offset = punctuation_payload["start_utf16_offset"]
                end_offset = punctuation_payload["end_utf16_offset"]
                replacement = punctuation_payload["replacement"]
                if (
                    not isinstance(paragraph_id, str)
                    or not isinstance(block_id, str)
                    or isinstance(start_offset, bool)
                    or not isinstance(start_offset, int)
                    or isinstance(end_offset, bool)
                    or not isinstance(end_offset, int)
                    or not isinstance(replacement, str)
                ):
                    raise ProjectError("schema 2 punctuation payload fields are invalid")
                revalidation_ms = _elapsed_ms(revalidation_started_at)
                validation_started_at = time.perf_counter()
                validate_draft_editor_snapshot(editor)
                validation_ms = _elapsed_ms(validation_started_at)
                child_write_started_at = time.perf_counter()
                workspace = edit_draft_workspace_punctuation(
                    editor.workflow.project_path,
                    run_id=self._cached_draft_workspace().checkpoint.workflow_run_id,
                    operation_id=operation_id,
                    expected_checkpoint_ref=checkpoint_ref,
                    expected_current_candidate_ref=current_ref,
                    paragraph_id=paragraph_id,
                    block_id=block_id,
                    start_utf16_offset=start_offset,
                    end_utf16_offset=end_offset,
                    replacement=replacement,
                    audit_review_session_id=state.review_session_id,
                    prepared_editor_snapshot=editor,
                )
                child_write_ms = _elapsed_ms(child_write_started_at)
                refresh_started_at = time.perf_counter()
                self._install_draft_workspace(editor.workflow, workspace)
                workflow_refresh_ms = _elapsed_ms(refresh_started_at)
                rebuild_started_at = time.perf_counter()
                refreshed = self._draft_editor_snapshot()
                draft_rebuild_ms = _elapsed_ms(rebuild_started_at)
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "schema_version": 2,
                        "operation": operation,
                        "draft_editor": self._draft_editor_payload(refreshed),
                        "timing": {
                            "selection_caret_revalidation_ms": revalidation_ms,
                            "immutable_child_write_fsync_ms": child_write_ms,
                            "project_brief_transcript_context_validation_ms": validation_ms,
                            "workflow_snapshot_refresh_ms": workflow_refresh_ms,
                            "draft_snapshot_rebuild_ms": draft_rebuild_ms,
                            "server_before_response_ms": _elapsed_ms(request_started_at),
                        },
                    },
                )
                return
            if operation in {
                "section_reorder",
                "section_rename",
                "section_split",
                "section_merge",
                "section_delete",
            }:
                _validate_payload_keys(
                    payload,
                    required={
                        "schema_version",
                        "operation_id",
                        "expected_checkpoint_ref",
                        "expected_current_candidate_ref",
                        "operation",
                        "payload",
                    },
                )
                if payload["schema_version"] != 2 or not isinstance(payload["payload"], dict):
                    raise ProjectError("schema 2 section envelope is invalid")
                operation_id, checkpoint_ref, current_ref = self._draft_workspace_envelope(payload)
                editor = self._draft_editor_snapshot_for_ref(current_ref)
                self._require_displayed_candidate(editor, {"candidate_id": current_ref.artifact_id})
                section_payload = payload["payload"]
                revalidation_ms = _elapsed_ms(revalidation_started_at)
                validation_started_at = time.perf_counter()
                validate_draft_editor_snapshot(editor)
                validation_ms = _elapsed_ms(validation_started_at)
                child_write_started_at = time.perf_counter()
                workspace = edit_draft_workspace_section(
                    editor.workflow.project_path,
                    run_id=self._cached_draft_workspace().checkpoint.workflow_run_id,
                    operation_id=operation_id,
                    expected_checkpoint_ref=checkpoint_ref,
                    expected_current_candidate_ref=current_ref,
                    operation=operation,
                    payload=section_payload,
                    audit_review_session_id=state.review_session_id,
                    prepared_editor_snapshot=editor,
                )
                child_write_ms = _elapsed_ms(child_write_started_at)
                refresh_started_at = time.perf_counter()
                self._install_draft_workspace(editor.workflow, workspace)
                workflow_refresh_ms = _elapsed_ms(refresh_started_at)
                rebuild_started_at = time.perf_counter()
                refreshed = self._draft_editor_snapshot()
                draft_rebuild_ms = _elapsed_ms(rebuild_started_at)
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "schema_version": 2,
                        "operation": operation,
                        "draft_editor": self._draft_editor_payload(refreshed),
                        "timing": {
                            "selection_caret_revalidation_ms": revalidation_ms,
                            "immutable_child_write_fsync_ms": child_write_ms,
                            "project_brief_transcript_context_validation_ms": validation_ms,
                            "workflow_snapshot_refresh_ms": workflow_refresh_ms,
                            "draft_snapshot_rebuild_ms": draft_rebuild_ms,
                            "server_before_response_ms": _elapsed_ms(request_started_at),
                        },
                    },
                )
                return
            _validate_payload_keys(
                payload,
                required={
                    "schema_version",
                    "operation_id",
                    "expected_checkpoint_ref",
                    "expected_current_candidate_ref",
                    "operation",
                    "accept_degraded",
                    "source",
                    "target",
                },
            )
            if payload["schema_version"] != 2 or operation not in {
                "move_selection",
                "insert_source_refs",
            }:
                raise ProjectError("schema 2 drop envelope is invalid")
            if not isinstance(payload["accept_degraded"], bool):
                raise ProjectError("schema 2 drop degraded acceptance is invalid")
            operation_id, checkpoint_ref, current_ref = self._draft_workspace_envelope(payload)
            editor = self._draft_editor_snapshot_for_ref(current_ref)
            self._require_displayed_candidate(editor, {"candidate_id": current_ref.artifact_id})
            source = payload["source"]
            target = payload["target"]
            if not isinstance(source, dict) or not isinstance(target, dict):
                raise ProjectError("schema 2 drop source and target must be objects")
            if operation == "move_selection":
                selection = self._resolve_schema2_drop_selection(editor, current_ref, source)
                if selection.resolution is None and payload["accept_degraded"]:
                    raise ProjectError("narration drop cannot accept degraded selection")
                if selection.resolution is not None and selection.resolution.degraded != bool(
                    source.get("degraded", False)
                ):
                    raise ProjectError("schema 2 drop degraded state changed")
                if (
                    selection.resolution is not None
                    and payload["accept_degraded"]
                    and not selection.resolution.degraded
                ):
                    raise ProjectError("schema 2 drop accepted a non-degraded selection")
                edit_operation = "move"
            else:
                selection = self._resolve_schema2_exact_source(editor, source)
                if payload["accept_degraded"] and (
                    selection.resolution is None
                    or not selection.resolution.degraded
                ):
                    raise ProjectError("schema 2 insert degraded acceptance is invalid")
                edit_operation = "insert"
            caret = self._resolve_schema2_drop_target(editor, target)
            revalidation_ms = _elapsed_ms(revalidation_started_at)
            validation_started_at = time.perf_counter()
            validate_draft_editor_snapshot(editor)
            validation_ms = _elapsed_ms(validation_started_at)
            child_write_started_at = time.perf_counter()
            workspace = edit_draft_workspace(
                editor.workflow.project_path,
                run_id=self._cached_draft_workspace().checkpoint.workflow_run_id,
                operation_id=operation_id,
                expected_checkpoint_ref=checkpoint_ref,
                expected_current_candidate_ref=current_ref,
                operation=edit_operation,
                selection=selection,
                caret=caret,
                accept_degraded=payload["accept_degraded"],
                audit_review_session_id=state.review_session_id,
                prepared_editor_snapshot=editor,
            )
            child_write_ms = _elapsed_ms(child_write_started_at)
            refresh_started_at = time.perf_counter()
            self._install_draft_workspace(editor.workflow, workspace)
            workflow_refresh_ms = _elapsed_ms(refresh_started_at)
            rebuild_started_at = time.perf_counter()
            refreshed = self._draft_editor_snapshot()
            draft_rebuild_ms = _elapsed_ms(rebuild_started_at)
            response_payload: dict[str, object] = {
                "schema_version": 2,
                "operation": operation,
                "selection": selection.to_dict(),
                "target": target,
                "draft_editor": self._draft_editor_payload(refreshed),
                "timing": {
                    "selection_caret_revalidation_ms": revalidation_ms,
                    "immutable_child_write_fsync_ms": child_write_ms,
                    "project_brief_transcript_context_validation_ms": validation_ms,
                    "workflow_snapshot_refresh_ms": workflow_refresh_ms,
                    "draft_snapshot_rebuild_ms": draft_rebuild_ms,
                    "server_before_response_ms": _elapsed_ms(request_started_at),
                },
            }
            if not workspace.readback and workspace.placement is not None:
                placement = workspace.placement
                result_selection = workspace.result_selection
                display_range = placement.display_range
                paragraph_ids = {
                    paragraph.get("paragraph_id")
                    for paragraph in refreshed.paragraphs
                }
                if (
                    display_range is None
                    or result_selection is None
                    or refreshed.candidate.content_draft_id != placement.candidate_id
                    or any(
                        point.get("paragraph_id") not in paragraph_ids
                        for point in display_range
                    )
                ):
                    raise ProjectError("draft editor placement changed after snapshot rebuild")
                response_payload["result_selection"] = result_selection
            self._send_json(
                HTTPStatus.CREATED,
                response_payload,
            )

        def _resolve_schema2_drop_selection(
            self,
            editor: DraftEditorSnapshot,
            current_ref: ArtifactRef,
            source: dict[str, Any],
        ):
            if source.get("kind") == "narration_block":
                return self._resolve_schema2_narration_block(editor, source)
            if (
                source.get("kind") != "resolved_selection"
                or source.get("surface") != "draft"
                or source.get("selection_kind") != "source_excerpt"
            ):
                raise ProjectError("schema 2 move source kind is invalid")
            _validate_payload_keys(
                source,
                required={
                    "kind", "resolution_hash", "surface", "selection_kind", "display_range",
                    "block_ids", "refs", "canonical_text", "degraded"
                },
            )
            display = source["display_range"]
            if not isinstance(display, dict):
                raise ProjectError("schema 2 move display range is invalid")
            _validate_payload_keys(display, required={"anchor", "focus"})
            anchor = self._schema2_display_point(editor, display["anchor"])
            focus = self._schema2_display_point(editor, display["focus"])
            selection = resolve_draft_editor_selection(
                editor, surface="draft", anchor=anchor, focus=focus
            )
            if not isinstance(source.get("degraded"), bool):
                raise ProjectError("schema 2 resolved selection degraded flag is invalid")
            if selection.resolution is None:
                raise ProjectError("schema 2 source selection unexpectedly resolved narration")
            payload_refs = source["refs"]
            if not isinstance(payload_refs, list) or not payload_refs or any(
                not isinstance(ref, dict)
                or set(ref)
                != {
                    "source_id",
                    "transcript_version_id",
                    "segment_id",
                    "start_ticks",
                    "end_ticks",
                }
                for ref in payload_refs
            ):
                raise ProjectError("schema 2 resolved selection refs are invalid")
            payload_block_ids = source["block_ids"]
            if (
                not isinstance(payload_block_ids, list)
                or not payload_block_ids
                or any(not isinstance(block_id, str) for block_id in payload_block_ids)
            ):
                raise ProjectError("schema 2 resolved selection block IDs are invalid")
            refs = [
                _schema2_ref_identity(ref)
                for ref in selection.resolution.refs
            ]
            block_ids: list[str] = []
            if selection.item_start is not None and selection.item_end is not None:
                for item in editor._items[selection.item_start : selection.item_end]:
                    block_id = getattr(item, "block_id", None)
                    if isinstance(block_id, str) and (not block_ids or block_ids[-1] != block_id):
                        block_ids.append(block_id)
            if refs != payload_refs or block_ids != payload_block_ids or selection.resolution.canonical_text != source["canonical_text"]:
                raise ProjectError("schema 2 resolved selection identity changed")
            if not isinstance(source["resolution_hash"], str) or len(source["resolution_hash"]) != 64:
                raise ProjectError("schema 2 resolution hash is invalid")
            hash_payload = dict(source)
            hash_payload.pop("resolution_hash", None)
            hash_payload["candidate_ref"] = current_ref.to_dict()
            if canonical_sha256_v1(hash_payload) != source["resolution_hash"]:
                raise ProjectError("schema 2 resolved selection hash is stale")
            return selection

        def _resolve_schema2_narration_block(
            self,
            editor: DraftEditorSnapshot,
            source: dict[str, Any],
        ) -> DraftEditorSelection:
            """Resolve a narration object envelope without any text resolver."""
            _validate_payload_keys(source, required={"kind", "surface", "block_id", "text", "status", "recorded_refs"})
            if (
                source["kind"] != "narration_block"
                or source["surface"] != "draft"
                or not isinstance(source["block_id"], str)
                or not source["block_id"]
            ):
                raise ProjectError("schema 2 narration source kind is invalid")
            matching = [
                item
                for item in editor.candidate.blocks
                if getattr(item, "block_id", None) == source["block_id"]
            ]
            if len(matching) != 1 or not isinstance(matching[0], NarrationBlock):
                raise ProjectError("schema 2 narration source block is not unique")
            block = matching[0]
            recorded = source["recorded_refs"]
            if not isinstance(recorded, list) or any(
                not isinstance(ref, dict)
                or set(ref)
                != {"source_id", "transcript_version_id", "segment_id", "start_ticks", "end_ticks"}
                for ref in recorded
            ):
                raise ProjectError("schema 2 narration recorded refs are invalid")
            if (
                block.text != source["text"]
                or block.status != source["status"]
                or [ref.to_dict() for ref in block.recorded_refs] != recorded
            ):
                raise ProjectError("schema 2 narration source identity changed")
            paragraph = next(
                (
                    item
                    for item in editor.paragraphs
                    if item.get("kind") == "narration" and item.get("block_id") == block.block_id
                ),
                None,
            )
            if paragraph is None:
                raise ProjectError("schema 2 narration source paragraph is missing")
            paragraph_id = str(paragraph["paragraph_id"])
            span = next((item for item in editor._spans if item.paragraph_id == paragraph_id), None)
            if span is None:
                raise ProjectError("schema 2 narration source span is missing")
            text = block.text
            return DraftEditorSelection(
                editor.candidate.content_draft_id,
                "draft",
                None,
                span.item_index,
                span.item_index + 1,
                ("narration", block.block_id),
                (),
                {"paragraph_id": paragraph_id, "character_offset": 0, "utf16_offset": 0},
                {
                    "paragraph_id": paragraph_id,
                    "character_offset": len(text),
                    "utf16_offset": codepoint_to_utf16_offset(text, len(text)),
                },
                block.block_id,
                block.text,
                block.status,
                block.recorded_refs,
            )

        def _resolve_schema2_exact_source(
            self,
            editor: DraftEditorSnapshot,
            source: dict[str, Any],
        ):
            _validate_payload_keys(
                source,
                required={"kind", "source_id", "transcript_version_id", "refs", "canonical_text"},
            )
            if source["kind"] != "exact_source_refs":
                raise ProjectError("schema 2 insert source kind is invalid")
            refs = source["refs"]
            if not isinstance(refs, list) or not refs:
                raise ProjectError("schema 2 insert refs are required")
            for ref in refs:
                if not isinstance(ref, dict) or set(ref) != {
                    "source_id",
                    "transcript_version_id",
                    "segment_id",
                    "start_ticks",
                    "end_ticks",
                }:
                    raise ProjectError("schema 2 insert refs are invalid")
            first = refs[0]
            last = refs[-1]
            if not isinstance(first, dict) or not isinstance(last, dict):
                raise ProjectError("schema 2 insert refs are invalid")
            if any(
                ref.get("source_id") != source["source_id"]
                or ref.get("transcript_version_id") != source["transcript_version_id"]
                for ref in refs
                if isinstance(ref, dict)
            ):
                raise ProjectError("schema 2 insert refs cross a binding")
            anchor = self._source_ref_point(editor, first, start=True)
            focus = self._source_ref_point(editor, last, start=False)
            selection = resolve_draft_editor_selection(
                editor,
                surface="source",
                anchor=anchor,
                focus=focus,
            )
            resolved_refs = (
                []
                if selection.resolution is None
                else [_schema2_ref_identity(ref) for ref in selection.resolution.refs]
            )
            if selection.resolution is None or resolved_refs != refs:
                raise ProjectError("schema 2 insert refs are stale")
            if selection.resolution.canonical_text != source["canonical_text"]:
                raise ProjectError("schema 2 insert canonical text is stale")
            return selection

        def _source_ref_point(
            self, editor: DraftEditorSnapshot, ref: dict[str, Any], *, start: bool
        ) -> dict[str, object]:
            key = (
                ref.get("source_id"),
                ref.get("transcript_version_id"),
                ref.get("segment_id"),
            )
            if not all(isinstance(item, str) for item in key):
                raise ProjectError("schema 2 source ref identity is invalid")
            try:
                paragraph_id, segment_offset = editor.transcript_base.paragraph_index[key]  # type: ignore[index]
                transcript = editor.transcript_base.transcripts[(key[0], key[1])]  # type: ignore[index]
                segment = next(item for item in transcript.segments if item.segment_id == key[2])
            except (KeyError, StopIteration) as error:
                raise ProjectError("schema 2 source ref segment does not exist") from error
            full_text = canonical_text_for_range(segment, segment.start_ticks, segment.end_ticks)
            spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
            ticks = ref.get("start_ticks" if start else "end_ticks")
            if not isinstance(ticks, int):
                raise ProjectError("schema 2 source ref ticks are invalid")
            if start and ticks == segment.start_ticks:
                local = 0
            elif not start and ticks == segment.end_ticks:
                local = len(full_text)
            else:
                try:
                    local = next(
                        (span.start_offset if start else span.end_offset)
                        for span in spans or ()
                        if (span.unit.start_ticks if start else span.unit.end_ticks) == ticks
                    )
                except StopIteration as error:
                    raise ProjectError("schema 2 source ref ticks are invalid") from error
            return {"paragraph_id": paragraph_id, "offset": segment_offset + local, "offset_encoding": "codepoint"}

        def _schema2_display_point(
            self, editor: DraftEditorSnapshot, value: object
        ) -> dict[str, object]:
            if not isinstance(value, dict) or set(value) != {"paragraph_id", "block_id", "utf16_offset"}:
                raise ProjectError("schema 2 display point fields are invalid")
            paragraph_id = value["paragraph_id"]
            block_id = value["block_id"]
            offset = value["utf16_offset"]
            if not isinstance(paragraph_id, str) or not isinstance(block_id, str) or isinstance(offset, bool) or not isinstance(offset, int):
                raise ProjectError("schema 2 display point is invalid")
            paragraph = next(
                (item for item in editor.paragraphs if item["paragraph_id"] == paragraph_id),
                None,
            )
            if paragraph is None:
                raise ProjectError("schema 2 display point paragraph is invalid")
            text = str(paragraph["text"])
            character_offset = utf16_to_codepoint_offset(text, offset)
            if paragraph.get("kind") == "narration":
                if paragraph.get("block_id") != block_id:
                    raise ProjectError("schema 2 narration display block is invalid")
            elif paragraph.get("kind") == "section_title":
                if paragraph.get("block_id") != block_id or character_offset != 0:
                    raise ProjectError("schema 2 section display block is invalid")
            else:
                if self._source_display_block_id(paragraph, character_offset) != block_id:
                    raise ProjectError("schema 2 display block is invalid")
            return {"paragraph_id": paragraph_id, "offset": offset, "offset_encoding": "utf16"}

        def _resolve_schema2_drop_target(
            self, editor: DraftEditorSnapshot, target: dict[str, Any]
        ):
            point = self._schema2_display_point(editor, target)
            return resolve_draft_editor_caret(
                editor,
                paragraph_id=str(point["paragraph_id"]),
                offset=cast(int, point["offset"]),
                offset_encoding="utf16",
            )

        def _update_draft_narration(self, payload: dict[str, Any]) -> None:
            _validate_payload_keys(
                payload,
                required={
                    "operation_id",
                    "expected_checkpoint_ref",
                    "expected_current_candidate_ref",
                    "candidate_id",
                    "block_id",
                    "text",
                },
            )
            operation_id, checkpoint_ref, current_ref = (
                self._draft_workspace_envelope(payload)
            )
            editor = self._draft_editor_snapshot_for_ref(current_ref)
            self._require_displayed_candidate(editor, payload)
            block_id = payload["block_id"]
            text = payload["text"]
            if not isinstance(block_id, str) or not isinstance(text, str):
                raise ProjectError("draft narration fields must be strings")
            workspace = edit_draft_workspace_narration(
                editor.workflow.project_path,
                run_id=self._cached_draft_workspace().checkpoint.workflow_run_id,
                operation_id=operation_id,
                expected_checkpoint_ref=checkpoint_ref,
                expected_current_candidate_ref=current_ref,
                block_id=block_id,
                text=text,
                audit_review_session_id=state.review_session_id,
                prepared_editor_snapshot=editor,
            )
            self._install_draft_workspace(editor.workflow, workspace)
            refreshed = self._draft_editor_snapshot()
            self._send_json(
                HTTPStatus.CREATED,
                {"draft_editor": self._draft_editor_payload(refreshed)},
            )

        def _search_draft_editor(self, payload: dict[str, Any]) -> None:
            _validate_payload_keys(
                payload,
                required={"candidate_id", "surface", "query", "offset", "limit"},
            )
            editor = self._draft_editor_snapshot()
            self._require_displayed_candidate(editor, payload)
            surface = payload["surface"]
            query = payload["query"]
            offset = payload["offset"]
            limit = payload["limit"]
            if (
                not isinstance(surface, str)
                or not isinstance(query, str)
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or isinstance(limit, bool)
                or not isinstance(limit, int)
            ):
                raise ProjectError("draft search fields are invalid")
            page = search_draft_editor(
                editor,
                surface=surface,
                query=query,
                offset=offset,
                limit=limit,
            )
            self._send_json(HTTPStatus.OK, page.to_dict())

        def _read_draft_transcript_window(
            self,
            payload: dict[str, Any],
        ) -> None:
            _validate_payload_keys(
                payload,
                required={
                    "candidate_id",
                    "source_id",
                    "offset",
                    "limit",
                },
                optional={"paragraph_id"},
            )
            editor = self._draft_editor_snapshot()
            self._require_displayed_candidate(editor, payload)
            source_id = payload["source_id"]
            offset = payload["offset"]
            limit = payload["limit"]
            paragraph_id = payload.get("paragraph_id")
            if (
                not isinstance(source_id, str)
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or (
                    paragraph_id is not None
                    and not isinstance(paragraph_id, str)
                )
            ):
                raise ProjectError("draft transcript window fields are invalid")
            window = read_draft_transcript_window(
                editor,
                source_id=source_id,
                offset=offset,
                limit=limit,
                paragraph_id=paragraph_id,
            )
            self._send_json(HTTPStatus.OK, window.to_dict())

        def _navigate_draft_candidate(
            self,
            payload: dict[str, Any],
            *,
            redo: bool,
        ) -> None:
            _validate_payload_keys(
                payload,
                required={
                    "operation_id",
                    "expected_checkpoint_ref",
                    "expected_current_candidate_ref",
                    "candidate_id",
                },
            )
            operation_id, checkpoint_ref, current_ref = (
                self._draft_workspace_envelope(payload)
            )
            candidate_id = payload.get("candidate_id")
            if candidate_id != current_ref.artifact_id:
                raise WorkflowError(
                    "draft_workspace_stale",
                    "Roughcut Draft workspace refused navigation for another candidate",
                )
            workflow = self._require_writable_workflow()
            workspace_before = self._cached_draft_workspace()
            navigate = redo_draft_workspace if redo else undo_draft_workspace
            workspace = navigate(
                workflow.project_path,
                run_id=workspace_before.checkpoint.workflow_run_id,
                operation_id=operation_id,
                expected_checkpoint_ref=checkpoint_ref,
                expected_current_candidate_ref=current_ref,
                audit_review_session_id=state.review_session_id,
            )
            self._install_draft_workspace(workflow, workspace)
            refreshed = self._draft_editor_snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "navigation": "redo" if redo else "undo",
                    "draft_editor": self._draft_editor_payload(refreshed),
                },
            )

        def _select_external_draft_candidate(
            self,
            payload: dict[str, Any],
        ) -> None:
            _validate_payload_keys(
                payload,
                required={
                    "operation_id",
                    "expected_checkpoint_ref",
                    "expected_current_candidate_ref",
                    "parent_candidate_id",
                    "child_candidate_id",
                },
            )
            workflow = self._require_writable_workflow()
            operation_id, checkpoint_ref, current_ref = (
                self._draft_workspace_envelope(payload)
            )
            parent_id = payload["parent_candidate_id"]
            child_id = payload["child_candidate_id"]
            if not isinstance(parent_id, str) or not isinstance(child_id, str):
                raise ProjectError("draft candidate handoff IDs must be strings")
            if parent_id != current_ref.artifact_id:
                raise ProjectError(
                    "draft candidate handoff parent is not the displayed candidate"
                )
            workspace_before = self._cached_draft_workspace()
            child_ref = ArtifactRef.from_dict(
                workflow_review_content_draft_ref(
                    workflow.project_path,
                    workspace_before.checkpoint.workflow_run_id,
                    child_id,
                )
            )
            workspace = select_draft_workspace_candidate(
                workflow.project_path,
                run_id=workspace_before.checkpoint.workflow_run_id,
                operation_id=operation_id,
                expected_checkpoint_ref=checkpoint_ref,
                expected_current_candidate_ref=current_ref,
                child_candidate_ref=child_ref,
                audit_review_session_id=state.review_session_id,
            )
            self._install_draft_workspace(workflow, workspace)
            refreshed = self._draft_editor_snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "handoff": {
                        "parent_candidate_id": parent_id,
                        "child_candidate_id": child_id,
                    },
                    "draft_editor": self._draft_editor_payload(refreshed),
                },
            )

        def _draft_editor_snapshot(self) -> DraftEditorSnapshot:
            workflow = self._require_workflow_review()
            workspace = self._require_draft_workspace()
            candidate_id = workspace.current_candidate.content_draft_id
            displayed_id = (
                workflow.content_draft.content_draft.content_draft_id
                if workflow.content_draft is not None
                else None
            )
            if displayed_id != candidate_id:
                raise WorkflowError(
                    "draft_workspace_stale",
                    "Roughcut Draft workspace refused a Review snapshot that no "
                    "longer displays its exact current candidate",
                )
            expected_key = DraftEditorCacheKey(
                workflow.project_revision,
                tuple(
                    (binding.source_id, binding.transcript_version_id)
                    for binding in workflow.source_bindings
                ),
                candidate_id,
            )
            with state.draft_editor_lock:
                cached = state.draft_editor_snapshot
                if cached is not None and cached.cache_key == expected_key:
                    validate_draft_editor_snapshot(cached)
                    return cached
                rebuilt = load_draft_editor_snapshot_from_workflow(
                    workflow,
                    content_draft_id=candidate_id,
                    transcript_base=self._compatible_transcript_base(workflow),
                )
                validate_draft_editor_snapshot(rebuilt)
                state.draft_editor_snapshot = rebuilt
                state.draft_editor_transcript_base = rebuilt.transcript_base
                return rebuilt

        def _cached_draft_editor_snapshot(self) -> DraftEditorSnapshot:
            with state.draft_editor_lock:
                cached = state.draft_editor_snapshot
                if cached is None:
                    raise WorkflowError(
                        "draft_workspace_transition_not_allowed",
                        "Roughcut Draft workspace has no editable Review snapshot",
                    )
                validate_draft_editor_snapshot(cached)
                return cached

        def _draft_editor_snapshot_for_ref(
            self,
            expected_ref: ArtifactRef,
        ) -> DraftEditorSnapshot:
            cached = self._cached_draft_editor_snapshot()
            if cached.candidate.content_draft_id == expected_ref.artifact_id:
                return cached
            workflow = self._require_writable_workflow()
            workspace = self._cached_draft_workspace()
            exact_ref = ArtifactRef.from_dict(
                workflow_review_content_draft_ref(
                    workflow.project_path,
                    workspace.checkpoint.workflow_run_id,
                    expected_ref.artifact_id,
                )
            )
            if exact_ref != expected_ref:
                raise WorkflowError(
                    "draft_workspace_integrity_error",
                    "Roughcut Draft workspace refused an expected candidate whose "
                    "exact ArtifactRef changed",
                )
            historical = load_workflow_review_snapshot(
                workflow.project_path,
                source_bindings=_workflow_bindings_payload(workflow),
                content_draft_id=expected_ref.artifact_id,
                playback_selections=workflow.playback_selections,
            )
            rebuilt = load_draft_editor_snapshot_from_workflow(
                historical,
                content_draft_id=expected_ref.artifact_id,
            )
            validate_draft_editor_snapshot(rebuilt)
            return rebuilt

        def _draft_editor_payload(
            self,
            editor: DraftEditorSnapshot,
        ) -> dict[str, object]:
            payload = editor.to_dict()
            workspace = self._require_draft_workspace()
            payload["history"] = {
                "can_undo": (
                    editor.candidate.content_draft_id
                    != self._workflow_draft_anchor_id(editor.workflow.project_path)
                ),
                "can_redo": bool(workspace.checkpoint.redo_candidate_refs),
                "redo_scope": "draft_workspace",
            }
            payload["workspace"] = {
                "expected_checkpoint_ref": workspace.checkpoint_ref.to_dict(),
                "expected_current_candidate_ref": (
                    workspace.checkpoint.current_candidate_ref.to_dict()
                ),
            }
            payload["candidate_handoff"] = {
                "endpoint": "/api/workflow/draft-candidate-select",
                "requires_exact_parent": True,
            }
            return payload

        def _require_draft_workspace(self) -> DraftWorkspaceState:
            workspace = state.draft_workspace
            if workspace is None:
                if state.draft_workspace_error is not None:
                    raise state.draft_workspace_error
                raise WorkflowError(
                    "draft_workspace_transition_not_allowed",
                    "Roughcut Draft workspace is not active for this Review page",
                )
            current = read_draft_workspace(
                state.snapshot.project_path,
                run_id=workspace.checkpoint.workflow_run_id,
            )
            if current is None:
                raise WorkflowError(
                    "draft_workspace_stale",
                    "Roughcut Draft workspace checkpoint is no longer available",
                )
            if current.checkpoint_ref != workspace.checkpoint_ref:
                raise WorkflowError(
                    "draft_workspace_stale",
                    "Roughcut Draft workspace was updated by another Review service",
                )
            return workspace

        def _cached_draft_workspace(self) -> DraftWorkspaceState:
            workspace = state.draft_workspace
            if workspace is None:
                if state.draft_workspace_error is not None:
                    raise state.draft_workspace_error
                raise WorkflowError(
                    "draft_workspace_transition_not_allowed",
                    "Roughcut Draft workspace is not active for this Review page",
                )
            return workspace

        def _draft_workspace_envelope(
            self,
            payload: dict[str, Any],
        ) -> tuple[str, DraftWorkspaceCheckpointRef, ArtifactRef]:
            operation_id = payload.get("operation_id")
            checkpoint_ref = DraftWorkspaceCheckpointRef.from_dict(
                payload.get("expected_checkpoint_ref")
            )
            match = (
                _DRAFT_WORKSPACE_OPERATION_ID.fullmatch(operation_id)
                if isinstance(operation_id, str)
                else None
            )
            if (
                match is None
                or int(match.group(1)) != checkpoint_ref.generation
            ):
                raise WorkflowError(
                    "draft_workspace_integrity_error",
                    "Roughcut Draft workspace requires a valid operation ID",
                )
            assert isinstance(operation_id, str)
            return (
                operation_id,
                checkpoint_ref,
                ArtifactRef.from_dict(
                    payload.get("expected_current_candidate_ref")
                ),
            )

        def _install_draft_workspace(
            self,
            workflow: WorkflowReviewSnapshot,
            workspace: DraftWorkspaceState,
        ) -> WorkflowReviewSnapshot:
            bindings = _workflow_bindings_payload(workflow)
            candidate = workspace.current_candidate
            if candidate.confirmed_by_user:
                refreshed = reopen_workflow_review_content_draft(
                    workflow.project_path,
                    source_bindings=bindings,
                    content_draft_id=candidate.content_draft_id,
                    playback_selections=workflow.playback_selections,
                )
            else:
                displayed = (
                    workflow.content_draft.content_draft
                    if workflow.content_draft is not None
                    else None
                )
                draft_state = (
                    load_workflow_review_content_draft_child(
                        workflow,
                        candidate.content_draft_id,
                        parent=displayed,
                    )
                    if (
                        displayed is not None
                        and candidate.parent_draft_id
                        == displayed.content_draft_id
                    )
                    else read_content_draft(
                        workflow.project_path,
                        candidate.content_draft_id,
                    )
                )
                refreshed = select_workflow_review_content_draft(
                    workflow,
                    draft_state,
                )
                refreshed = replace(
                    refreshed,
                    reopened_parent_draft_id=workflow.reopened_parent_draft_id,
                )
            rebuilt = load_draft_editor_snapshot_from_workflow(
                refreshed,
                content_draft_id=candidate.content_draft_id,
                transcript_base=self._compatible_transcript_base(refreshed),
            )
            validate_draft_editor_snapshot(rebuilt)
            with state.draft_editor_lock:
                state.snapshot = refreshed
                state.draft_workspace = workspace
                state.draft_workspace_error = None
                state.draft_editor_snapshot = rebuilt
                state.draft_editor_transcript_base = rebuilt.transcript_base
            return refreshed

        def _workflow_draft_anchor_id(self, project_path: Path) -> str:
            status = self._finite_workflow_status(project_path)
            run = status["workflow_run"]
            if not isinstance(run, dict):
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut workflow façade returned an invalid WorkflowRun",
                )
            refs = run.get("artifact_refs")
            draft_ref = refs.get("content_draft") if isinstance(refs, dict) else None
            if not isinstance(draft_ref, dict) or not isinstance(
                draft_ref.get("artifact_id"), str
            ):
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut workflow façade omitted the current Draft anchor",
                )
            artifact_id = draft_ref["artifact_id"]
            assert isinstance(artifact_id, str)
            return artifact_id

        def _draft_agent_handoff(self, payload: dict[str, Any]) -> None:
            _validate_payload_keys(
                payload,
                required={"candidate_id"},
                optional={"quote"},
            )
            editor = self._draft_editor_snapshot()
            self._require_displayed_candidate(editor, payload)
            quote = payload.get("quote")
            if quote is not None and not isinstance(quote, str):
                raise ProjectError("draft Agent handoff quote must be a string or null")
            normalized_quote = quote.strip() if isinstance(quote, str) else ""
            if len(normalized_quote) > 2_000:
                raise ProjectError("draft Agent handoff quote is too long")
            workflow = editor.workflow
            candidate_id = editor.candidate.content_draft_id
            lines = [
                "Transcript Rough Cut 初稿调整交接",
                f"项目：{workflow.project_id}",
                f"Review session：{state.review_session_id}",
                f"Content Draft candidate：{candidate_id}",
            ]
            if normalized_quote:
                lines.extend(("当前选区引文：", normalized_quote))
            lines.extend(
                (
                    (
                        "请先读取上述精确 candidate，再解释或执行修改；"
                        "不得扫描目录猜测最新稿。"
                    ),
                    (
                        "如果“第一段、开头、刚才那段”等指代首次解析出的目标"
                        "已经删除、移动或替换，请停止并询问，不得自动改绑。"
                    ),
                    "修改内容：",
                )
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "handoff": {
                        "candidate_id": candidate_id,
                        "session_id": state.review_session_id,
                        "text": "\n".join(lines),
                    }
                },
            )

        def _require_displayed_candidate(
            self,
            editor: DraftEditorSnapshot,
            payload: dict[str, Any],
        ) -> None:
            candidate_id = payload.get("candidate_id")
            if not isinstance(candidate_id, str):
                raise ProjectError("draft editor candidate ID must be a string")
            if candidate_id != editor.candidate.content_draft_id:
                raise ProjectError("draft editor candidate is stale")

        def _compatible_transcript_base(
            self,
            workflow: WorkflowReviewSnapshot,
        ) -> DraftEditorTranscriptBase | None:
            base = state.draft_editor_transcript_base
            bindings = tuple(
                (binding.source_id, binding.transcript_version_id)
                for binding in workflow.source_bindings
            )
            if (
                base is None
                or base.project_revision != workflow.project_revision
                or base.source_bindings != bindings
            ):
                return None
            return base

        def _read_proposal_coverage(self, payload: dict[str, Any]) -> None:
            current = self._require_artifact_review()
            if current.basis_type != "proposal":
                raise ProjectError("Proposal coverage requires a Proposal review")
            _validate_payload_keys(
                payload,
                required={"offset", "limit"},
                optional={"filters"},
            )
            offset = payload["offset"]
            limit = payload["limit"]
            filters = payload.get("filters")
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise ProjectError("Proposal coverage offset must be an integer")
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ProjectError("Proposal coverage limit must be an integer")
            if filters is not None and not isinstance(filters, dict):
                raise ProjectError("Proposal coverage filters must be an object")
            page = read_readable_transcript(
                current.project_path,
                source_bindings=_artifact_bindings_payload(current),
                expected_revision=current.project_revision,
                offset=offset,
                limit=limit,
                filters=filters,
                overlay={"basis": "proposal", "artifact_id": current.basis_id},
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "artifact": {
                        "basis": "proposal",
                        "artifact_id": current.basis_id,
                        "content_draft_id": state.workflow_content_draft_id,
                    },
                    "readable_transcript": page.to_dict(),
                },
            )

        def _read_proposal_diff(self, *, head_only: bool) -> None:
            if not isinstance(state.snapshot, ReviewSnapshot):
                self._json_error(
                    HTTPStatus.BAD_REQUEST,
                    "proposal_diff_not_applicable",
                    "Proposal diff requires a Proposal review",
                )
                return
            snapshot = state.snapshot
            project = ProjectStore(snapshot.project_path).load()
            if snapshot.basis_type != "proposal":
                self._json_error(
                    HTTPStatus.BAD_REQUEST,
                    "proposal_diff_not_applicable",
                    "Proposal diff requires a Proposal review",
                )
                return
            if project.active_edit_version_id is None:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "applicable": False,
                        "reason": "no_active_decision",
                        "project_revision": project.revision,
                    },
                    head_only=head_only,
                )
                return
            try:
                diff = read_proposal_diff(
                    snapshot.project_path,
                    snapshot.basis_id,
                    expected_revision=snapshot.project_revision,
                )
            except ProjectError as error:
                self._json_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_proposal_diff",
                    str(error),
                )
                return
            payload = {"applicable": True, **diff.to_dict()}
            self._send_json(HTTPStatus.OK, payload, head_only=head_only)

        def _displayed_content_draft_id(
            self,
            workflow: WorkflowReviewSnapshot,
            payload: dict[str, Any],
        ) -> str:
            content_draft_id = payload["content_draft_id"]
            if not isinstance(content_draft_id, str):
                raise ProjectError("workflow Content Draft ID must be a string")
            displayed_id = (
                workflow.content_draft.content_draft.content_draft_id
                if workflow.content_draft is not None
                else None
            )
            if content_draft_id != displayed_id:
                raise ProjectError("workflow change must reference the displayed Content Draft")
            return content_draft_id

        def _finite_workflow_status(self, project_path: Path) -> dict[str, Any]:
            status = workflow_status(project_path)
            return dict(status)

        def _workflow_request_key(
            self,
            action: str,
            payload: dict[str, object],
        ) -> str:
            return action + ":" + json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        def _perform_workflow_action(
            self,
            project: Path | WorkflowReviewSnapshot,
            action: str,
            action_input: dict[str, object],
            *,
            request_payload: dict[str, object] | None = None,
        ) -> WorkflowFacadeResult:
            project_path = (
                project.project_path
                if isinstance(project, WorkflowReviewSnapshot)
                else project
            )
            with state.workflow_action_lock:
                status = self._finite_workflow_status(project_path)
                run = status["workflow_run"]
                if not isinstance(run, dict) or not isinstance(run.get("run_id"), str):
                    raise WorkflowError(
                        "workflow_integrity_error",
                        "Roughcut workflow façade returned an invalid WorkflowRun",
                    )
                key = self._workflow_request_key(
                    action,
                    action_input if request_payload is None else request_payload,
                )
                remembered = state.workflow_action_requests.get(key)
                if remembered is None:
                    action_id = "act_review_" + secrets.token_hex(12)
                    state.workflow_action_requests[key] = (action_id, action_input)
                else:
                    action_id, remembered_input = remembered
                    if remembered_input != action_input:
                        raise WorkflowError(
                            "workflow_action_conflict",
                            "Roughcut workflow façade detected a Review idempotency "
                            "request with different action input",
                        )
                return workflow_action(
                    project_path,
                    run["run_id"],
                    action_id,
                    action,
                    action_input,
                )

        def _repeat_workflow_action(
            self,
            project_path: Path,
            action: str,
            request_payload: dict[str, object],
        ) -> WorkflowFacadeResult:
            with state.workflow_action_lock:
                key = self._workflow_request_key(action, request_payload)
                remembered = state.workflow_action_requests.get(key)
                if remembered is None:
                    raise WorkflowError(
                        "workflow_transition_not_allowed",
                        "Roughcut workflow façade has no matching Review action to read back",
                    )
                action_id, action_input = remembered
                status = self._finite_workflow_status(project_path)
                run = status["workflow_run"]
                return workflow_action(
                    project_path,
                    run["run_id"],
                    action_id,
                    action,
                    action_input,
                )

        def _closed_workflow_block(
            self, block: dict[str, Any]
        ) -> dict[str, object]:
            normalized = dict(block)
            if normalized.get("kind") == "narration":
                normalized.setdefault("recorded_refs", [])
            return normalized

        def _read_workflow_artifact(
            self,
            project_path: Path,
            result: WorkflowFacadeResult,
            kind: str,
        ) -> dict[str, object]:
            ref = result.workflow_run.artifact_refs[kind]
            if ref is None:
                raise WorkflowError(
                    "workflow_integrity_error",
                    f"Roughcut workflow façade receipt omitted the {kind} artifact ref",
                )
            directory = {
                "brief": "briefs",
            }.get(kind)
            if directory is None:
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut Review requested an unsupported workflow artifact",
                )
            return read_json_object(
                project_path / directory / f"{ref.artifact_id}.json",
                description=f"workflow Review {kind}",
            )

        def _send_confirmed_draft_result(
            self,
            result: WorkflowFacadeResult,
            requested_id: str,
        ) -> None:
            receipt = result.receipt
            mutation = receipt.mutation if receipt is not None else None
            proposal_ref = result.workflow_run.artifact_refs["proposal"]
            if (
                receipt is None
                or mutation is None
                or mutation.kind != "content_draft"
                or proposal_ref is None
            ):
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut workflow façade returned an invalid approve_draft receipt",
                )
            current = state.snapshot
            project_path = current.project_path
            playback = current.playback_selections
            bindings = (
                _workflow_bindings_payload(current)
                if isinstance(current, WorkflowReviewSnapshot)
                else _artifact_bindings_payload(current)
            )
            confirmed_workflow = load_workflow_review_snapshot(
                project_path,
                source_bindings=bindings,
                content_draft_id=mutation.artifact_id,
                playback_selections=playback,
            )
            confirmed = read_content_draft(
                project_path, mutation.artifact_id
            ).content_draft
            if (
                confirmed.parent_draft_id != requested_id
                or not confirmed.confirmed_by_user
            ):
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut workflow façade confirmed child does not match "
                    "the displayed Draft",
                )
            state.snapshot = load_review_snapshot(
                project_path,
                proposal_id=proposal_ref.artifact_id,
                playback_selections=playback,
            )
            assert isinstance(state.snapshot, ReviewSnapshot)
            state.draft_editor_snapshot = None
            state.draft_editor_transcript_base = None
            state.draft_workspace = None
            state.workflow_content_draft_id = mutation.artifact_id
            state.workflow_proposal_id = proposal_ref.artifact_id
            self._send_json(
                HTTPStatus.OK,
                {
                    "content_draft_mutation": {
                        "content_draft": confirmed.to_dict(),
                        "project_revision": receipt.after.project_revision,
                    },
                    "workflow_receipt": receipt.to_dict(),
                    "workflow": workflow_review_payload(confirmed_workflow),
                },
            )

        def _confirm(self, payload: dict[str, Any]) -> None:
            self._require_writable_review()
            current = self._require_artifact_review()
            proposal_id = payload.get("proposal_id")
            if not isinstance(proposal_id, str) or proposal_id != current.basis_id:
                raise ProjectError("confirmation must reference the displayed proposal")
            self._adopt_roughcut_candidate(
                {
                    "basis_id": proposal_id,
                    "expected_revision": current.project_revision,
                }
            )

        def _reject(self, payload: dict[str, Any]) -> None:
            del payload
            raise WorkflowError(
                "workflow_transition_not_allowed",
                "Roughcut workflow façade rejected direct Review Proposal rejection; "
                "return the current roughcut through return_to_draft",
            )

        def _correct_transcript(self, payload: dict[str, Any]) -> None:
            current = self._require_artifact_review()
            source_id = self._authorized_source_id(payload)
            parent_id = payload.get("parent_transcript_version_id")
            corrections = payload.get("corrections")
            expected_revision = payload.get("expected_revision")
            if not isinstance(parent_id, str):
                raise ProjectError("parent transcript version is required")
            if not isinstance(corrections, list) or not all(
                isinstance(correction, dict) for correction in corrections
            ):
                raise ProjectError("transcript corrections must be an array of objects")
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ProjectError("expected revision must be an integer")
            mutation = correct_transcript(
                current.project_path,
                source_id=source_id,
                parent_transcript_version_id=parent_id,
                corrections=corrections,
                expected_revision=expected_revision,
            )
            response = _transcript_review_payload(current)
            response["transcript_mutation"] = mutation.to_dict()
            self._send_json(HTTPStatus.OK, response)

        def _activate_transcript(self, payload: dict[str, Any]) -> None:
            current = self._require_artifact_review()
            source_id = self._authorized_source_id(payload)
            transcript_id = payload.get("transcript_version_id")
            expected_revision = payload.get("expected_revision")
            if not isinstance(transcript_id, str):
                raise ProjectError("transcript version is required")
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ProjectError("expected revision must be an integer")
            activation = activate_transcript_version(
                current.project_path,
                source_id=source_id,
                transcript_version_id=transcript_id,
                expected_revision=expected_revision,
            )
            response = _transcript_review_payload(current)
            response["transcript_activation"] = activation.to_dict()
            self._send_json(HTTPStatus.OK, response)

        def _change_edit(self, payload: dict[str, Any]) -> None:
            current = self._require_decision_review()
            self._require_writable_review()
            active_id, expected_revision = self._edit_write_basis(payload)
            operation = payload.get("operation")
            if not isinstance(operation, dict):
                raise ProjectError("edit operation must be an object")
            changed = change_edit(
                current.project_path,
                operation=operation,
                expected_revision=expected_revision,
                base_edit_version_id=active_id,
            )
            state.snapshot = load_review_snapshot(
                current.project_path,
                edit_version_id=changed.decision.edit_version_id,
                playback_selections=current.playback_selections,
            )
            assert isinstance(state.snapshot, ReviewSnapshot)
            response = _review_edit_payload(state.snapshot)
            response["edit_change"] = changed.to_dict()
            self._send_json(HTTPStatus.OK, response)

        def _change_roughcut_candidate(self, payload: dict[str, Any]) -> None:
            self._require_writable_review()
            current = self._require_artifact_review()
            _validate_payload_keys(
                payload,
                required={"basis_id", "expected_revision", "operation"},
            )
            self._require_roughcut_basis(payload, current)
            operation = payload["operation"]
            if not isinstance(operation, dict):
                raise ProjectError("roughcut operation must be an object")
            restoration_clips = _remember_latest_clips(
                current.proposal.clips,
                state.roughcut_restoration_clips,
            )
            changed = propose_review_edit_change(
                current.project_path,
                proposal=current.proposal,
                operation=operation,
                expected_revision=current.project_revision,
                restoration_clips=restoration_clips,
            )
            if changed.changed:
                proposal_path = (
                    current.project_path
                    / "proposals"
                    / f"{changed.proposal.proposal_id}.json"
                )
                try:
                    next_snapshot = load_review_snapshot(
                        current.project_path,
                        proposal_id=changed.proposal.proposal_id,
                        playback_selections=current.playback_selections,
                    )
                except Exception:
                    proposal_path.unlink(missing_ok=True)
                    raise
                state.roughcut_undo.append((current.basis_type, current.basis_id))
                state.roughcut_redo.clear()
                state.roughcut_restoration_clips = restoration_clips
                state.snapshot = next_snapshot
                assert isinstance(state.snapshot, ReviewSnapshot)
            response = self._roughcut_state_payload()
            response["candidate_change"] = changed.to_dict()
            self._send_json(
                HTTPStatus.CREATED if changed.changed else HTTPStatus.OK,
                response,
            )

        def _navigate_roughcut_candidate(
            self,
            payload: dict[str, Any],
            *,
            redo: bool,
        ) -> None:
            self._require_writable_review()
            current = self._require_artifact_review()
            _validate_payload_keys(
                payload,
                required={"basis_id", "expected_revision"},
            )
            self._require_roughcut_basis(payload, current)
            source = state.roughcut_redo if redo else state.roughcut_undo
            target = state.roughcut_undo if redo else state.roughcut_redo
            if not source:
                raise ProjectError(
                    "roughcut candidate has no state to redo"
                    if redo
                    else "roughcut candidate has no state to undo"
                )
            pointer = source.pop()
            target.append((current.basis_type, current.basis_id))
            try:
                state.snapshot = self._load_roughcut_pointer(current, pointer)
            except Exception:
                target.pop()
                source.append(pointer)
                raise
            self._send_json(HTTPStatus.OK, self._roughcut_state_payload())

        def _adopt_roughcut_candidate(self, payload: dict[str, Any]) -> None:
            self._require_writable_review()
            current = self._require_artifact_review()
            _validate_payload_keys(
                payload,
                required={"basis_id", "expected_revision"},
            )
            basis_id = payload["basis_id"]
            expected_revision = payload["expected_revision"]
            if not isinstance(basis_id, str):
                raise ProjectError("roughcut basis ID must be a string")
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ProjectError("expected revision must be an integer")
            if (
                current.basis_type == "decision"
                and (
                    basis_id == current.basis_id
                    or basis_id == state.roughcut_last_adopted_proposal_id
                )
            ):
                result = self._repeat_workflow_action(
                    current.project_path,
                    "adopt_roughcut",
                    payload,
                )
                if result.receipt is None:
                    raise WorkflowError(
                        "workflow_integrity_error",
                        "Roughcut workflow façade omitted the adopt_roughcut receipt",
                    )
                response = self._roughcut_state_payload()
                response["adoption"] = {
                    "changed": False,
                    "project_revision": result.receipt.after.project_revision,
                }
                response["workflow_receipt"] = result.receipt.to_dict()
                self._send_json(HTTPStatus.OK, response)
                return
            if (
                current.basis_type != "proposal"
                or basis_id != current.basis_id
                or expected_revision != current.project_revision
            ):
                raise ProjectError("adoption must reference the displayed roughcut candidate")
            status = self._finite_workflow_status(current.project_path)
            proposal_ref = status["presented_subjects"]["proposal_ref"]
            if (
                not isinstance(proposal_ref, dict)
                or proposal_ref.get("artifact_id") != current.basis_id
            ):
                raise WorkflowError(
                    "workflow_subject_mismatch",
                    "Roughcut workflow façade rejected a Review Proposal that "
                    "is not the current workflow subject",
                )
            result = self._perform_workflow_action(
                current.project_path,
                "adopt_roughcut",
                {
                    "schema_version": 1,
                    "proposal_ref": proposal_ref,
                },
                request_payload=payload,
            )
            if result.receipt is None:
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut workflow façade omitted the adopt_roughcut receipt",
                )
            decision_ref = result.workflow_run.artifact_refs["decision"]
            if decision_ref is None:
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut workflow façade omitted the adopted Decision ref",
                )
            state.roughcut_last_adopted_proposal_id = current.basis_id
            state.snapshot = load_review_snapshot(
                current.project_path,
                edit_version_id=decision_ref.artifact_id,
                playback_selections=current.playback_selections,
            )
            assert isinstance(state.snapshot, ReviewSnapshot)
            state.roughcut_undo.clear()
            state.roughcut_redo.clear()
            state.roughcut_restoration_clips = state.snapshot.proposal.clips
            response = self._roughcut_state_payload()
            response["adoption"] = {
                "changed": True,
                "project_revision": result.receipt.after.project_revision,
            }
            response["workflow_receipt"] = result.receipt.to_dict()
            self._send_json(HTTPStatus.OK, response)

        def _return_roughcut_to_draft(self, payload: dict[str, Any]) -> None:
            if isinstance(state.snapshot, WorkflowReviewSnapshot):
                workflow = self._require_writable_workflow()
                result = self._repeat_workflow_action(
                    workflow.project_path,
                    "return_to_draft",
                    payload,
                )
                if result.receipt is None:
                    raise WorkflowError(
                        "workflow_integrity_error",
                        "Roughcut workflow façade omitted the return_to_draft receipt",
                    )
                editor = self._draft_editor_snapshot()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "draft_editor": self._draft_editor_payload(editor),
                        "workflow_receipt": result.receipt.to_dict(),
                    },
                )
                return
            current = self._require_artifact_review()
            self._require_writable_review()
            _validate_payload_keys(
                payload,
                required={"basis_id", "expected_revision"},
            )
            self._require_roughcut_basis(payload, current)
            content_draft_id = state.workflow_content_draft_id
            if content_draft_id is None:
                raise ProjectError(
                    "roughcut preview is not linked to a Content Draft Review session"
                )
            status = self._finite_workflow_status(current.project_path)
            subjects = status["presented_subjects"]
            subject_ref = subjects["return_subject_ref"]
            draft_ref = subjects["confirmed_content_draft_ref"]
            if (
                not isinstance(subject_ref, dict)
                or subject_ref.get("artifact_id") != current.basis_id
                or not isinstance(draft_ref, dict)
                or draft_ref.get("artifact_id") != content_draft_id
            ):
                raise WorkflowError(
                    "workflow_subject_mismatch",
                    "Roughcut workflow façade rejected Review return subjects "
                    "that are not current",
                )
            result = self._perform_workflow_action(
                current.project_path,
                "return_to_draft",
                {
                    "schema_version": 1,
                    "current_subject_ref": subject_ref,
                    "confirmed_content_draft_ref": draft_ref,
                },
                request_payload=payload,
            )
            if result.receipt is None:
                raise WorkflowError(
                    "workflow_integrity_error",
                    "Roughcut workflow façade omitted the return_to_draft receipt",
                )
            workflow = reopen_workflow_review_content_draft(
                current.project_path,
                source_bindings=_artifact_bindings_payload(current),
                content_draft_id=content_draft_id,
                playback_selections=current.playback_selections,
            )
            workspace = open_draft_workspace(
                current.project_path,
                run_id=result.workflow_run.run_id,
                audit_review_session_id=state.review_session_id,
            )
            self._install_draft_workspace(workflow, workspace)
            editor = self._draft_editor_snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "draft_editor": self._draft_editor_payload(editor),
                    "workflow_receipt": result.receipt.to_dict(),
                },
            )

        def _require_roughcut_basis(
            self,
            payload: dict[str, Any],
            current: ReviewSnapshot,
        ) -> None:
            basis_id = payload["basis_id"]
            expected_revision = payload["expected_revision"]
            if not isinstance(basis_id, str) or basis_id != current.basis_id:
                raise ProjectError("roughcut change must reference the displayed candidate")
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision != current.project_revision
            ):
                raise ProjectError("project revision conflict")

        def _load_roughcut_pointer(
            self,
            current: ReviewSnapshot,
            pointer: tuple[str, str],
        ) -> ReviewSnapshot:
            basis_type, basis_id = pointer
            if basis_type == "proposal":
                return load_review_snapshot(
                    current.project_path,
                    proposal_id=basis_id,
                    playback_selections=current.playback_selections,
                )
            if basis_type == "decision":
                return load_review_snapshot(
                    current.project_path,
                    edit_version_id=basis_id,
                    playback_selections=current.playback_selections,
                )
            raise ProjectError("roughcut history basis is invalid")

        def _roughcut_state_payload(self) -> dict[str, object]:
            current = self._require_artifact_review()
            restorable = read_review_restorable_clips(
                current.project_path,
                proposal=current.proposal,
                restoration_clips=state.roughcut_restoration_clips,
            )
            return {
                "review": current.to_dict(),
                "candidate_history": {
                    "can_undo": bool(state.roughcut_undo),
                    "can_redo": bool(state.roughcut_redo),
                    "restorable_clips": [clip.to_dict() for clip in restorable],
                },
            }

        def _navigate_edit(self, payload: dict[str, Any], *, redo: bool) -> None:
            current = self._require_decision_review()
            self._require_writable_review()
            active_id, expected_revision = self._edit_write_basis(payload)
            navigation: EditNavigationState = (redo_edit if redo else undo_edit)(
                current.project_path,
                expected_revision=expected_revision,
                base_edit_version_id=active_id,
            )
            state.snapshot = load_review_snapshot(
                current.project_path,
                edit_version_id=navigation.active_edit_version_id,
                playback_selections=current.playback_selections,
            )
            assert isinstance(state.snapshot, ReviewSnapshot)
            response = _review_edit_payload(state.snapshot)
            response["edit_navigation"] = navigation.to_dict()
            self._send_json(HTTPStatus.OK, response)

        def _edit_write_basis(self, payload: dict[str, Any]) -> tuple[str, int]:
            active_id = payload.get("active_edit_version_id")
            expected_revision = payload.get("expected_revision")
            if not isinstance(active_id, str):
                raise ProjectError("active edit version is required")
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ProjectError("expected revision must be an integer")
            return active_id, expected_revision

        def _require_artifact_review(self) -> ReviewSnapshot:
            if not isinstance(state.snapshot, ReviewSnapshot):
                raise ProjectError("operation requires a Proposal or Decision review")
            return state.snapshot

        def _require_decision_review(self) -> ReviewSnapshot:
            artifact = self._require_artifact_review()
            if artifact.basis_type != "decision":
                raise ProjectError("direct edit history requires a Decision review")
            return artifact

        def _require_workflow_review(self) -> WorkflowReviewSnapshot:
            if not isinstance(state.snapshot, WorkflowReviewSnapshot):
                raise ProjectError("workflow review is not active")
            return state.snapshot

        def _require_writable_workflow(self) -> WorkflowReviewSnapshot:
            workflow = self._require_workflow_review()
            session = workflow_session_status(workflow)
            if session.read_only:
                raise ProjectError("workflow review is stale and read-only")
            return workflow

        def _authorized_source_id(self, payload: dict[str, Any]) -> str:
            source_id = payload.get("source_id")
            if (
                not isinstance(source_id, str)
                or _SAFE_ID.fullmatch(source_id) is None
                or source_id not in state.snapshot.authorized_source_ids
            ):
                raise ProjectError("transcript source is not authorized by this review")
            return source_id

        def _require_writable_review(self) -> None:
            if isinstance(state.snapshot, WorkflowReviewSnapshot):
                self._require_writable_workflow()
                return
            project = ProjectStore(state.snapshot.project_path).load()
            if (
                project.revision != state.snapshot.project_revision
                or _snapshot_bindings_stale(state.snapshot, project=project)
                or _snapshot_active_edit_stale(state.snapshot, project=project)
            ):
                raise ProjectError(
                    "review is stale; return to the Agent and create a new Proposal from active Transcripts"
                )

        def _read_json(self) -> dict[str, Any]:
            length_text = self.headers.get("Content-Length")
            if length_text is None:
                raise ValueError("missing body")
            length = int(length_text)
            if length < 0 or length > _MAX_JSON_BODY:
                raise ValueError("invalid body size")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise TypeError("JSON body must be an object")
            return data

        def _send_json(
            self, status: HTTPStatus, payload: dict[str, object], *, head_only: bool = False
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self._send_bytes(
                status,
                body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                head_only=head_only,
            )

        def _json_error(
            self,
            status: HTTPStatus,
            code: str,
            message: str,
            *,
            current_revision: int | None = None,
        ) -> None:
            error: dict[str, object] = {"code": code, "message": message}
            if current_revision is not None:
                error["current_revision"] = current_revision
            payload = health()
            payload["ok"] = False
            payload["error"] = error
            self._send_json(status, payload)

        def _send_bytes(
            self,
            status: HTTPStatus,
            body: bytes,
            *,
            headers: dict[str, str],
            head_only: bool,
        ) -> None:
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; media-src 'self'; script-src 'self'; style-src 'self'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

    return ReviewHandler


def _review_edit_payload(snapshot: ReviewSnapshot) -> dict[str, object]:
    history = read_edit_history(snapshot.project_path)
    if (
        history.project_revision != snapshot.project_revision
        or history.active_edit_version_id != snapshot.basis_id
    ):
        raise ProjectError("review edit history changed while reading")
    return {
        "review": snapshot.to_dict(),
        "edit_history": history.to_dict(),
    }


def _transcript_review_payload(snapshot: ReviewSnapshot) -> dict[str, object]:
    store = ProjectStore(snapshot.project_path)
    project = store.load()
    source_payloads: list[dict[str, object]] = []
    edit_reference_status: dict[str, object] | None = None
    for frozen_source in snapshot.sources:
        source_id = str(frozen_source["source_id"])
        versions = read_transcript_versions(snapshot.project_path, source_id=source_id)
        if versions.project_revision != project.revision:
            raise ProjectError("project revision changed while reading transcript versions")
        current_edit_status = versions.edit_reference_status.to_dict()
        if edit_reference_status is None:
            edit_reference_status = current_edit_status
        elif edit_reference_status != current_edit_status:
            raise ProjectError("active edit status changed while reading transcript versions")
        active_segments = (
            _active_transcript_segments(
                snapshot.project_path,
                source_id,
                versions.active_transcript_version_id,
            )
            if versions.active_transcript_version_id is not None
            else []
        )
        source_payloads.append(
            {
                "source_id": source_id,
                "display_name": frozen_source["display_name"],
                "frozen_transcript_version_id": frozen_source["transcript_version_id"],
                "active_transcript_version_id": versions.active_transcript_version_id,
                "versions": [version.to_dict() for version in versions.versions],
                "active_segments": active_segments,
            }
        )
    if ProjectStore(snapshot.project_path).load().revision != project.revision:
        raise ProjectError("project revision changed while reading transcript versions")

    review_mismatches = [
        {
            "source_id": source["source_id"],
            "referenced_transcript_version_id": source["frozen_transcript_version_id"],
            "active_transcript_version_id": source["active_transcript_version_id"],
        }
        for source in source_payloads
        if source["frozen_transcript_version_id"] != source["active_transcript_version_id"]
    ]
    session_stale = project.revision != snapshot.project_revision or bool(review_mismatches)
    return {
        "project_revision": project.revision,
        "sources": source_payloads,
        "edit_reference_status": edit_reference_status or {"status": "none", "mismatches": []},
        "review_session": {
            "status": "stale" if session_stale else "current",
            "read_only": session_stale,
            "snapshot_revision": snapshot.project_revision,
            "current_revision": project.revision,
            "mismatches": review_mismatches,
        },
    }


def _active_transcript_segments(
    project_path: Path,
    source_id: str,
    transcript_version_id: str,
) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    offset = 0
    while True:
        page = read_transcript_page(
            project_path,
            source_id,
            transcript_version_id,
            offset=offset,
            limit=200,
        )
        segments.extend(
            {
                "segment_id": segment["segment_id"],
                "start_ticks": segment["start_ticks"],
                "end_ticks": segment["end_ticks"],
                "original_text": segment["original_text"],
                "corrected_text": segment["corrected_text"],
            }
            for segment in page.segments
        )
        if page.next_offset is None:
            return segments
        offset = page.next_offset


def _snapshot_bindings_stale(
    snapshot: ReviewSnapshot | WorkflowReviewSnapshot,
    *,
    project: Project | None = None,
) -> bool:
    current = project or ProjectStore(snapshot.project_path).load()
    return any(
        current.active_transcript_versions.get(str(source["source_id"]))
        != source["transcript_version_id"]
        for source in snapshot.sources
    )


def _snapshot_active_edit_stale(
    snapshot: ReviewSnapshot | WorkflowReviewSnapshot,
    *,
    project: Project | None = None,
) -> bool:
    if isinstance(snapshot, WorkflowReviewSnapshot):
        return False
    if snapshot.basis_type != "decision":
        return False
    current = project or ProjectStore(snapshot.project_path).load()
    return current.active_edit_version_id != snapshot.basis_id


def _remember_latest_clips(
    latest: tuple[EditClip, ...],
    existing: tuple[EditClip, ...],
) -> tuple[EditClip, ...]:
    remembered: dict[str, EditClip] = {}
    for clip in (*latest, *existing):
        remembered.setdefault(clip.clip_id, clip)
    return tuple(remembered.values())


def _workflow_bindings_payload(
    snapshot: WorkflowReviewSnapshot,
) -> list[dict[str, object]]:
    return [binding.to_dict() for binding in snapshot.source_bindings]


def _artifact_bindings_payload(snapshot: ReviewSnapshot) -> list[dict[str, object]]:
    proposal = snapshot.proposal
    if isinstance(proposal, MultiSourceEditProposal):
        return [binding.to_dict() for binding in proposal.source_bindings]
    return [
        {
            "source_id": proposal.source_id,
            "transcript_version_id": proposal.transcript_version_id,
        }
    ]


def _validate_payload_keys(
    payload: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    if set(payload) - allowed:
        raise ProjectError("workflow request contains unknown fields")
    if required - set(payload):
        raise ProjectError("workflow request is missing required fields")


def _registered_media_path(project_path: Path, source: SourceAsset) -> Path:
    if source.import_mode is ImportMode.COPIED:
        relative = source.locator.get("project_relative_path")
        if relative is None:
            raise ProjectError("copied media locator is missing")
        resolved = (project_path / relative).resolve(strict=True)
        if not resolved.is_relative_to(project_path):
            raise ProjectError("copied media path escapes the project")
    else:
        absolute = source.locator.get("absolute_path")
        if absolute is None:
            raise ProjectError("linked media locator is missing")
        resolved = Path(absolute).resolve(strict=True)
        if str(resolved) != str(Path(absolute).resolve()):
            raise ProjectError("linked media path is invalid")
    if not resolved.is_file():
        raise ProjectError("registered media is not a file")
    return resolved


def _parse_range(value: str, size: int) -> tuple[int, int]:
    match = _RANGE.fullmatch(value)
    if match is None or size <= 0:
        raise ProjectError("unsupported media range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ProjectError("unsupported media range")
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ProjectError("unsupported media range")
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise ProjectError("unsupported media range")
    return start, min(end, size - 1)
