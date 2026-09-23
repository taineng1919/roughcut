"""Minimal local stdio MCP adapter around roughcut application services."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from roughcut import __version__
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
from roughcut.adapters.qwen import WORKSPACE_ID_PATTERN
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
from roughcut.application.health import TOOL_SCHEMA_VERSION, health
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
from roughcut.m2_7_public_capability import require_m2_7_public_capability

PROTOCOL_VERSION = "2025-03-26"
TRANSPORT_BUSY_ERROR = -32000


class _InvalidArgumentType(ValueError, TypeError):
    """Retain the MCP's closed ValueError envelope for type-invalid input."""


_ResultT = TypeVar("_ResultT")


class _UnexpectedOperationError(Exception):
    """Mark an ordinary operation error for the stable MCP tool envelope."""


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


CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clip_id": {"type": "string", "minLength": 1},
        "source_id": {"type": "string", "minLength": 1},
        "transcript_version_id": {"type": "string", "minLength": 1},
        "segment_id": {"type": "string", "minLength": 1},
        "source_in_ticks": {"type": "integer", "minimum": 0},
        "source_out_ticks": {"type": "integer", "minimum": 1},
        "reason": {"type": "string", "minLength": 1},
        "display_text": {"type": "string", "minLength": 1},
    },
    "required": [
        "clip_id",
        "source_id",
        "transcript_version_id",
        "segment_id",
        "source_in_ticks",
        "source_out_ticks",
        "reason",
        "display_text",
    ],
    "additionalProperties": False,
}

SOURCE_BINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "minLength": 1},
        "transcript_version_id": {"type": "string", "minLength": 1},
    },
    "required": ["source_id", "transcript_version_id"],
    "additionalProperties": False,
}

TRANSCRIPT_CORRECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "segment_id": {"type": "string", "minLength": 1},
        "corrected_text": {"type": ["string", "null"]},
    },
    "required": ["segment_id", "corrected_text"],
    "additionalProperties": False,
}

TRANSCRIPT_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "paragraph_id": {"type": "string", "minLength": 1},
        "start_offset": {"type": "integer", "minimum": 0},
        "end_offset": {"type": "integer", "minimum": 1},
        "quote": {"type": "string", "minLength": 1},
        "occurrence": {"type": "integer", "minimum": 0},
    },
    "required": ["paragraph_id"],
    "additionalProperties": False,
}

CONTENT_DRAFT_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "minLength": 1},
        "transcript_version_id": {"type": "string", "minLength": 1},
        "segment_id": {"type": "string", "minLength": 1},
        "start_ticks": {"type": "integer", "minimum": 0},
        "end_ticks": {"type": "integer", "minimum": 1},
    },
    "required": [
        "source_id",
        "transcript_version_id",
        "segment_id",
        "start_ticks",
        "end_ticks",
    ],
    "additionalProperties": False,
}

CONTENT_DRAFT_BLOCK_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "block_id": {"type": "string", "minLength": 1},
                "kind": {"const": "source_excerpt"},
                "refs": {
                    "type": "array",
                    "minItems": 1,
                    "items": CONTENT_DRAFT_REF_SCHEMA,
                },
                "canonical_text": {"type": "string", "minLength": 1},
            },
            "required": ["block_id", "kind", "refs"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "block_id": {"type": "string", "minLength": 1},
                "kind": {"const": "narration"},
                "text": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string",
                    "enum": ["draft", "approved", "recorded"],
                },
                "recorded_refs": {
                    "type": "array",
                    "items": CONTENT_DRAFT_REF_SCHEMA,
                },
            },
            "required": ["block_id", "kind", "text", "status", "recorded_refs"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "block_id": {"type": "string", "minLength": 1},
                "kind": {"const": "section_title"},
                "title": {"type": "string", "minLength": 1, "maxLength": 80},
            },
            "required": ["block_id", "kind", "title"],
            "additionalProperties": False,
        },
    ]
}

_WORKFLOW_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^[A-Za-z0-9_-]{1,128}$",
}
_WORKFLOW_HASH_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^[a-f0-9]{64}$",
}

_MAIN_CAMERA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "camera_id": {"const": "main"},
        "ordered_source_ids": {
            "type": "array",
            "minItems": 1,
            "items": _WORKFLOW_ID_SCHEMA,
        },
    },
    "required": ["camera_id", "ordered_source_ids"],
    "additionalProperties": False,
}
_SOURCE_PAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "main_source_id": _WORKFLOW_ID_SCHEMA,
        "auxiliary_source_id": _WORKFLOW_ID_SCHEMA,
    },
    "required": ["main_source_id", "auxiliary_source_id"],
    "additionalProperties": False,
}
_AUXILIARY_CAMERA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "camera_id": {
            **_WORKFLOW_ID_SCHEMA,
            "not": {"const": "main"},
        },
        "ordered_source_ids": {
            "type": "array",
            "minItems": 1,
            "items": _WORKFLOW_ID_SCHEMA,
        },
        "source_pairs": {
            "type": "array",
            "minItems": 1,
            "items": _SOURCE_PAIR_SCHEMA,
        },
    },
    "required": ["camera_id", "ordered_source_ids"],
    "additionalProperties": False,
}
_ALIGNMENT_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"const": "multicam_alignment"},
        "alignment_id": _WORKFLOW_ID_SCHEMA,
        "schema_version": {"const": 1},
        "content_hash": _WORKFLOW_HASH_SCHEMA,
    },
    "required": ["kind", "alignment_id", "schema_version", "content_hash"],
    "additionalProperties": False,
}
_PARALLEL_PREPARE_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"const": 1},
        "prepare_id": _WORKFLOW_ID_SCHEMA,
        "project_id": _WORKFLOW_ID_SCHEMA,
        "project_revision": {"type": "integer", "minimum": 0},
        "decision_ref": {
            "type": "object",
            "properties": {
                "kind": {"const": "decision"},
                "edit_version_id": _WORKFLOW_ID_SCHEMA,
                "schema_version": {"type": "integer", "minimum": 1},
                "content_hash": _WORKFLOW_HASH_SCHEMA,
            },
            "required": ["kind", "edit_version_id", "schema_version", "content_hash"],
            "additionalProperties": False,
        },
        "decision_adoption": {
            "type": "object",
            "properties": {
                "run_id": _WORKFLOW_ID_SCHEMA,
                "receipt_ref": {
                    "type": "object",
                    "properties": {
                        "action_id": _WORKFLOW_ID_SCHEMA,
                        "receipt_schema_version": {"const": 1},
                        "receipt_hash": _WORKFLOW_HASH_SCHEMA,
                    },
                    "required": ["action_id", "receipt_schema_version", "receipt_hash"],
                    "additionalProperties": False,
                },
                "approval_ref": {
                    "type": "object",
                    "properties": {
                        "approval_id": _WORKFLOW_ID_SCHEMA,
                        "record_schema_version": {"const": 1},
                        "record_hash": _WORKFLOW_HASH_SCHEMA,
                    },
                    "required": ["approval_id", "record_schema_version", "record_hash"],
                    "additionalProperties": False,
                },
            },
            "required": ["run_id", "receipt_ref", "approval_ref"],
            "additionalProperties": False,
        },
        "alignment_ref": _ALIGNMENT_REF_SCHEMA,
        "alignment_producer": {
            "type": "object",
            "properties": {
                "operation_id": _WORKFLOW_ID_SCHEMA,
                "operation_type": {"const": "align_multicam"},
                "result_ref": _ALIGNMENT_REF_SCHEMA,
            },
            "required": ["operation_id", "operation_type", "result_ref"],
            "additionalProperties": False,
        },
        "auxiliary_camera_ids": {
            "type": "array",
            "minItems": 1,
            "items": _WORKFLOW_ID_SCHEMA,
        },
        "output_settings_hash": _WORKFLOW_HASH_SCHEMA,
        "plan_basis_hash": _WORKFLOW_HASH_SCHEMA,
    },
    "required": [
        "schema_version", "prepare_id", "project_id", "project_revision",
        "decision_ref", "decision_adoption", "alignment_ref", "alignment_producer",
        "auxiliary_camera_ids", "output_settings_hash", "plan_basis_hash",
    ],
    "additionalProperties": False,
}
_WORKFLOW_ARTIFACT_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_id": _WORKFLOW_ID_SCHEMA,
        "schema_version": {"type": "integer", "minimum": 1},
        "content_hash": _WORKFLOW_HASH_SCHEMA,
    },
    "required": ["artifact_id", "schema_version", "content_hash"],
    "additionalProperties": False,
}
_WORKFLOW_BINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_id": _WORKFLOW_ID_SCHEMA,
        "transcript_version_id": _WORKFLOW_ID_SCHEMA,
    },
    "required": ["source_id", "transcript_version_id"],
    "additionalProperties": False,
}
_WORKFLOW_RANGE_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_WORKFLOW_BINDING_SCHEMA["properties"],
        "segment_id": _WORKFLOW_ID_SCHEMA,
        "start_ticks": {"type": "integer", "minimum": 0},
        "end_ticks": {"type": "integer", "minimum": 1},
    },
    "required": [
        "source_id",
        "transcript_version_id",
        "segment_id",
        "start_ticks",
        "end_ticks",
    ],
    "additionalProperties": False,
}
_MULTICAM_AUTHORIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_id": _WORKFLOW_ID_SCHEMA,
        "transcribe": {"type": "boolean"},
        "speaker_diarization": {"type": "boolean"},
    },
    "required": ["source_id", "transcribe", "speaker_diarization"],
    "additionalProperties": False,
}
_MULTICAM_CAMERA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "camera_id": _WORKFLOW_ID_SCHEMA,
        "ordered_source_ids": {
            "type": "array",
            "minItems": 1,
            "items": _WORKFLOW_ID_SCHEMA,
        },
    },
    "required": ["camera_id", "ordered_source_ids"],
    "additionalProperties": False,
}
_MULTICAM_SOURCE_PAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "main_source_id": _WORKFLOW_ID_SCHEMA,
        "auxiliary_source_id": _WORKFLOW_ID_SCHEMA,
    },
    "required": ["main_source_id", "auxiliary_source_id"],
    "additionalProperties": False,
}
_MULTICAM_SETUP_DECLARATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"const": 1},
        "main_camera": _MULTICAM_CAMERA_SCHEMA,
        "auxiliary_cameras": {
            "type": "array",
            "items": _MULTICAM_CAMERA_SCHEMA,
        },
        "source_pairs": {
            "type": "array",
            "items": _MULTICAM_SOURCE_PAIR_SCHEMA,
        },
    },
    "required": [
        "schema_version",
        "main_camera",
        "auxiliary_cameras",
        "source_pairs",
    ],
    "additionalProperties": False,
}
_APPROVE_SCOPE_LEGACY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"const": 1},
        "confirmation_basis": {
            "type": "object",
            "properties": {
                "basis_id": {
                    "type": "string",
                    "pattern": "^wfb_scope_[a-f0-9]{64}$",
                }
            },
            "required": ["basis_id"],
            "additionalProperties": False,
        },
        "source_authorizations": {
            "type": "array",
            "minItems": 1,
            "items": _MULTICAM_AUTHORIZATION_SCHEMA,
        },
    },
    "required": [
        "schema_version",
        "confirmation_basis",
        "source_authorizations",
    ],
    "additionalProperties": False,
}
_WORKFLOW_ACTION_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "approve_scope": {
        "type": "object",
        "properties": {
            **_APPROVE_SCOPE_LEGACY_SCHEMA["properties"],
            "multicam_setup": _MULTICAM_SETUP_DECLARATION_SCHEMA,
        },
        "required": _APPROVE_SCOPE_LEGACY_SCHEMA["required"],
        "additionalProperties": False,
        "oneOf": [
            {"not": {"required": ["multicam_setup"]}},
            {"required": ["multicam_setup"]},
        ],
    },
    "confirm_brief": {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "confirmation_basis": {
                "type": "object",
                "properties": {
                    "basis_id": {
                        "type": "string",
                        "pattern": "^wfb_brief_[a-f0-9]{64}$",
                    }
                },
                "required": ["basis_id"],
                "additionalProperties": False,
            },
            "theme": {"type": "string", "minLength": 1},
            "target_duration_ticks": {"type": "integer", "minimum": 1},
            "focus": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "allow_reorder": {"type": "boolean"},
            "speaker_resolution_waivers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_id": _WORKFLOW_ID_SCHEMA,
                        "transcript_version_id": _WORKFLOW_ID_SCHEMA,
                        "local_speaker_id": _WORKFLOW_ID_SCHEMA,
                    },
                    "required": [
                        "source_id",
                        "transcript_version_id",
                        "local_speaker_id",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "schema_version",
            "confirmation_basis",
            "theme",
            "target_duration_ticks",
            "focus",
            "allow_reorder",
            "speaker_resolution_waivers",
        ],
        "additionalProperties": False,
    },
    "submit_outline": {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "title": {"type": "string", "minLength": 1},
            "opening": {"type": "string", "minLength": 1},
            "sections": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "section_id": _WORKFLOW_ID_SCHEMA,
                        "title": {"type": "string", "minLength": 1},
                        "summary": {"type": "string", "minLength": 1},
                        "target_duration_ticks": {
                            "type": "integer",
                            "minimum": 1,
                        },
                    },
                    "required": [
                        "section_id",
                        "title",
                        "summary",
                        "target_duration_ticks",
                    ],
                    "additionalProperties": False,
                },
            },
            "ending": {"type": "string", "minLength": 1},
            "required_content_coverage": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement": {"type": "string", "minLength": 1},
                        "covered": {"type": "boolean"},
                        "evidence_refs": {
                            "type": "array",
                            "items": _WORKFLOW_RANGE_REF_SCHEMA,
                        },
                    },
                    "required": ["requirement", "covered", "evidence_refs"],
                    "additionalProperties": False,
                },
            },
            "narration_status": {
                "type": "string",
                "enum": ["none", "pending", "to_write", "recorded"],
            },
        },
        "required": [
            "schema_version",
            "title",
            "opening",
            "sections",
            "ending",
            "required_content_coverage",
            "narration_status",
        ],
        "additionalProperties": False,
    },
    "approve_outline": {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "outline_ref": _WORKFLOW_ARTIFACT_REF_SCHEMA,
        },
        "required": ["schema_version", "outline_ref"],
        "additionalProperties": False,
    },
    "submit_draft": {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "parent_draft_ref": {
                "oneOf": [_WORKFLOW_ARTIFACT_REF_SCHEMA, {"type": "null"}]
            },
            "display_title": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 80,
            },
            "source_bindings": {
                "type": "array",
                "minItems": 1,
                "items": _WORKFLOW_BINDING_SCHEMA,
            },
            "brief_ref": _WORKFLOW_ARTIFACT_REF_SCHEMA,
            "context_hash": _WORKFLOW_HASH_SCHEMA,
            "blocks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "block_id": _WORKFLOW_ID_SCHEMA,
                                "kind": {"const": "source_excerpt"},
                                "refs": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": _WORKFLOW_RANGE_REF_SCHEMA,
                                },
                                "canonical_text": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                            },
                            "required": [
                                "block_id",
                                "kind",
                                "refs",
                                "canonical_text",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "block_id": _WORKFLOW_ID_SCHEMA,
                                "kind": {"const": "narration"},
                                "text": {"type": "string", "minLength": 1},
                                "status": {
                                    "type": "string",
                                    "enum": ["draft", "approved", "recorded"],
                                },
                                "recorded_refs": {
                                    "type": "array",
                                    "items": _WORKFLOW_RANGE_REF_SCHEMA,
                                },
                            },
                            "required": [
                                "block_id",
                                "kind",
                                "text",
                                "status",
                                "recorded_refs",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "block_id": _WORKFLOW_ID_SCHEMA,
                                "kind": {"const": "section_title"},
                                "title": {"type": "string", "minLength": 1, "maxLength": 80},
                            },
                            "required": ["block_id", "kind", "title"],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "scoped_mutable_block_ids": {
                "type": "array",
                "items": _WORKFLOW_ID_SCHEMA,
            },
        },
        "required": [
            "schema_version",
            "parent_draft_ref",
            "display_title",
            "source_bindings",
            "brief_ref",
            "context_hash",
            "blocks",
            "scoped_mutable_block_ids",
        ],
        "additionalProperties": False,
    },
    "approve_draft": {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "content_draft_ref": _WORKFLOW_ARTIFACT_REF_SCHEMA,
        },
        "required": ["schema_version", "content_draft_ref"],
        "additionalProperties": False,
    },
    "return_to_draft": {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "current_subject_ref": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["proposal", "decision"]},
                    **_WORKFLOW_ARTIFACT_REF_SCHEMA["properties"],
                },
                "required": [
                    "kind",
                    "artifact_id",
                    "schema_version",
                    "content_hash",
                ],
                "additionalProperties": False,
            },
            "confirmed_content_draft_ref": _WORKFLOW_ARTIFACT_REF_SCHEMA,
        },
        "required": [
            "schema_version",
            "current_subject_ref",
            "confirmed_content_draft_ref",
        ],
        "additionalProperties": False,
    },
    "adopt_roughcut": {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "proposal_ref": _WORKFLOW_ARTIFACT_REF_SCHEMA,
        },
        "required": ["schema_version", "proposal_ref"],
        "additionalProperties": False,
    },
    "approve_export": {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "export_ref": _WORKFLOW_ARTIFACT_REF_SCHEMA,
        },
        "required": ["schema_version", "export_ref"],
        "additionalProperties": False,
    },
}


def _workflow_action_schema() -> dict[str, object]:
    envelope_properties = {
        "project_path": {"type": "string", "minLength": 1},
        "run_id": _WORKFLOW_ID_SCHEMA,
        "action_id": _WORKFLOW_ID_SCHEMA,
    }
    required = ["project_path", "run_id", "action_id", "action", "input"]
    return {
        "type": "object",
        "properties": {
            **envelope_properties,
            "action": {
                "type": "string",
                "enum": list(_WORKFLOW_ACTION_INPUT_SCHEMAS),
            },
            "input": {"type": "object"},
        },
        "required": required,
        "additionalProperties": False,
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    **envelope_properties,
                    "action": {"const": action},
                    "input": input_schema,
                },
                "required": required,
                "additionalProperties": False,
            }
            for action, input_schema in _WORKFLOW_ACTION_INPUT_SCHEMAS.items()
        ]
    }


EDIT_OPERATION_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "type": {"const": "delete"},
                "clip_id": {"type": "string", "minLength": 1},
            },
            "required": ["type", "clip_id"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"const": "restore"},
                "clip_id": {"type": "string", "minLength": 1},
                "insert_before_clip_id": {"type": ["string", "null"]},
            },
            "required": ["type", "clip_id"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"const": "reorder"},
                "ordered_clip_ids": {
                    "type": "array",
                    "items": _WORKFLOW_ID_SCHEMA,
                },
            },
            "required": ["type", "ordered_clip_ids"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"const": "trim"},
                "clip_id": {"type": "string", "minLength": 1},
                "source_in_ticks": {"type": "integer", "minimum": 0},
                "source_out_ticks": {"type": "integer", "minimum": 1},
            },
            "required": ["type", "clip_id", "source_in_ticks", "source_out_ticks"],
            "additionalProperties": False,
        },
    ]
}

TOOLS = (
    {
        "name": "health",
        "description": "Return roughcut core health information.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "diagnostics",
        "description": (
            "Return read-only runtime, BBC alignment, FFmpeg, and FunASR diagnostics."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "qwen_credential_configure",
        "description": (
            "Store or replace the current user's Qwen Filetrans API Key and "
            "Workspace ID in Roughcut's private credential file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "minLength": 1},
                "workspace_id": {
                    "type": "string",
                    "pattern": f"^{WORKSPACE_ID_PATTERN.pattern}$",
                },
            },
            "required": ["api_key", "workspace_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qwen_credential_readiness",
        "description": (
            "Return local Qwen credential readiness without contacting the provider."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "qwen_credential_clear",
        "description": (
            "Remove the current user's stored Qwen Filetrans credential record."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "workflow_start",
        "description": "Explicitly start one finite WorkflowRun.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "run_id": _WORKFLOW_ID_SCHEMA,
                "ordered_source_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": ["project_path", "run_id", "ordered_source_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "workflow_status",
        "description": "Return the validated closed finite-workflow status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "run_id": _WORKFLOW_ID_SCHEMA,
            },
            "required": ["project_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "media_operation_status",
        "description": "Read or converge one closed Project-media operation record.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "operation_id": _WORKFLOW_ID_SCHEMA,
            },
            "required": ["project_path", "operation_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "approve_nle_export",
        "description": "Approve and atomically publish one exact editable NLE handoff file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "run_id": _WORKFLOW_ID_SCHEMA,
                "action_id": _WORKFLOW_ID_SCHEMA,
                "edit_version_id": _WORKFLOW_ID_SCHEMA,
                "expected_revision": {"type": "integer", "minimum": 0},
                "route": {"type": "string", "enum": ["fcpxml", "fcp7_xml"]},
                "destination": {"type": "string", "minLength": 1},
                "alignment_artifact_id": {
                    "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}]
                },
            },
            "required": [
                "project_path", "run_id", "action_id", "edit_version_id",
                "expected_revision", "route", "destination", "alignment_artifact_id",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "align_multicam",
        "description": "Run one tracked fixed-offset multicam alignment operation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "operation_id": _WORKFLOW_ID_SCHEMA,
                "alignment_id": _WORKFLOW_ID_SCHEMA,
                "expected_revision": {"type": "integer", "minimum": 0},
                "main_camera": _MAIN_CAMERA_SCHEMA,
                "auxiliary_cameras": {
                    "type": "array",
                    "minItems": 1,
                    "items": _AUXILIARY_CAMERA_SCHEMA,
                },
                "main_audio_stable": {"const": True},
                "max_temporary_disk_bytes": {"type": "integer", "minimum": 1},
                "max_analysis_memory_bytes": {"type": "integer", "minimum": 1},
                "max_runtime_seconds": {"type": "integer", "minimum": 1},
            },
            "required": [
                "project_path", "operation_id", "alignment_id", "expected_revision",
                "main_camera", "auxiliary_cameras", "main_audio_stable",
                "max_temporary_disk_bytes", "max_analysis_memory_bytes", "max_runtime_seconds",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "multicam_parallel_render_prepare",
        "description": "Purely project the current adopted Decision onto a delivered alignment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "edit_version_id": _WORKFLOW_ID_SCHEMA,
                "alignment_ref": _ALIGNMENT_REF_SCHEMA,
                "auxiliary_camera_ids": {
                    "type": "array",
                    "minItems": 1,
                    "items": _WORKFLOW_ID_SCHEMA,
                },
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path", "edit_version_id", "alignment_ref",
                "auxiliary_camera_ids", "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "multicam_parallel_render_start",
        "description": "Start one approved parallel multicam render from an exact prepare ref.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "operation_id": _WORKFLOW_ID_SCHEMA,
                "prepare_ref": _PARALLEL_PREPARE_REF_SCHEMA,
            },
            "required": ["project_path", "operation_id", "prepare_ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "workflow_action",
        "description": "Execute exactly one frozen finite-workflow business action.",
        "inputSchema": _workflow_action_schema(),
    },
    {
        "name": "workflow_cancel",
        "description": "Cancel one active finite WorkflowRun with an idempotent receipt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "run_id": _WORKFLOW_ID_SCHEMA,
                "action_id": _WORKFLOW_ID_SCHEMA,
            },
            "required": ["project_path", "run_id", "action_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fake_project_roundtrip",
        "description": "Create, read and remove a media-free temporary project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_name": {"type": "string", "minLength": 1}},
            "required": ["project_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "project_create",
        "description": "Create a minimal roughcut project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "output_preset": {
                    "type": "string",
                    "enum": ["landscape_1080p", "portrait_1080p"],
                },
            },
            "required": ["project_path", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "project_open",
        "description": "Open a roughcut project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_path": {"type": "string", "minLength": 1}},
            "required": ["project_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "source_add",
        "description": "Copy or link a source into a roughcut project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_path": {"type": "string", "minLength": 1},
                "import_mode": {"type": "string", "enum": ["copied", "linked"]},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "source_path", "import_mode", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "person_create",
        "description": "Create one project person with an explicit role and note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "role": {"type": "string", "minLength": 1},
                "note": {"type": "string"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "name", "role", "note", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "source_metadata_update",
        "description": "Update one source's optional friendly name, normalized tags, and note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "display_name": {"type": "string", "minLength": 1},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "note": {"type": "string"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "source_id", "tags", "note", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "speaker_map_confirm",
        "description": "Confirm or explicitly remap one transcript-local speaker to a person.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "transcript_version_id": {"type": "string", "minLength": 1},
                "local_speaker_id": {"type": "string", "minLength": 1},
                "person_id": {"type": "string", "minLength": 1},
                "confirmed_by_user": {"type": "boolean"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "source_id",
                "transcript_version_id",
                "local_speaker_id",
                "person_id",
                "confirmed_by_user",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "people_read",
        "description": "Read project people, source metadata, and confirmed speaker maps.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_path": {"type": "string", "minLength": 1}},
            "required": ["project_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "transcribe_source",
        "description": "Transcribe one authorized Source using its canonical ASR route.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "operation_id": _WORKFLOW_ID_SCHEMA,
                "source_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "speaker_diarization": {"type": "boolean", "default": False},
            },
            "required": [
                "project_path",
                "operation_id",
                "source_id",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "transcript_page",
        "description": "Read one stable-ID page from a timed transcript.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "transcript_version_id": {"type": "string", "minLength": 1},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": [
                "project_path",
                "source_id",
                "transcript_version_id",
                "offset",
                "limit",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "readable_transcript_read",
        "description": "Derive a stable, paged, path-free readable transcript view.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_bindings": {
                    "type": "array",
                    "minItems": 1,
                    "items": SOURCE_BINDING_SCHEMA,
                },
                "expected_revision": {"type": "integer", "minimum": 0},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "filters": {"type": "object"},
                "overlay": {"type": "object"},
            },
            "required": [
                "project_path",
                "source_bindings",
                "expected_revision",
                "offset",
                "limit",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "transcript_selection_resolve",
        "description": "Resolve readable paragraph selections to exact or explicit segment-expanded refs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_bindings": {
                    "type": "array",
                    "minItems": 1,
                    "items": SOURCE_BINDING_SCHEMA,
                },
                "expected_revision": {"type": "integer", "minimum": 0},
                "view_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "selections": {
                    "type": "array",
                    "minItems": 1,
                    "items": TRANSCRIPT_SELECTION_SCHEMA,
                },
                "overlay": {"type": "object"},
            },
            "required": [
                "project_path",
                "source_bindings",
                "expected_revision",
                "view_hash",
                "selections",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "markdown_export",
        "description": "Atomically export a readable transcript or existing Proposal/Decision script and map.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "basis": {"type": "string", "enum": ["transcript", "proposal", "decision"]},
                "output_path": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "source_bindings": {
                    "type": "array",
                    "minItems": 1,
                    "items": SOURCE_BINDING_SCHEMA,
                },
                "artifact_id": {"type": "string", "minLength": 1},
            },
            "required": ["project_path", "basis", "output_path", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "content_draft_create",
        "description": "Create an immutable unconfirmed Content Draft candidate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "parent_draft_id": {"type": ["string", "null"]},
                "display_title": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "maxLength": 80,
                },
                "source_bindings": {
                    "type": "array",
                    "minItems": 1,
                    "items": SOURCE_BINDING_SCHEMA,
                },
                "brief_id": {"type": "string", "minLength": 1},
                "context_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "blocks": {
                    "type": "array",
                    "minItems": 1,
                    "items": CONTENT_DRAFT_BLOCK_SCHEMA,
                },
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "source_bindings",
                "brief_id",
                "context_hash",
                "blocks",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "content_draft_revise_scoped",
        "description": (
            "Create an immutable child while core preserves every parent block "
            "outside the declared mutable scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "parent_draft_id": {"type": "string", "minLength": 1},
                "mutable_block_ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "blocks": {
                    "type": "array",
                    "minItems": 1,
                    "items": CONTENT_DRAFT_BLOCK_SCHEMA,
                },
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "parent_draft_id",
                "mutable_block_ids",
                "blocks",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "content_draft_read",
        "description": "Read one candidate or historical confirmed Content Draft and stale state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "content_draft_id": {"type": "string", "minLength": 1},
            },
            "required": ["project_path", "content_draft_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "content_draft_confirm",
        "description": "Create and activate an immutable user-confirmed Content Draft child.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "content_draft_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "content_draft_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "content_draft_propose",
        "description": "Compile the active current fully recorded Content Draft through Proposal services.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "content_draft_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "content_draft_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "transcript_correct",
        "description": "Create and activate an immutable text-correction transcript child.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "parent_transcript_version_id": {"type": "string", "minLength": 1},
                "corrections": {
                    "type": "array",
                    "minItems": 1,
                    "items": TRANSCRIPT_CORRECTION_SCHEMA,
                },
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "source_id",
                "parent_transcript_version_id",
                "corrections",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "transcript_version_activate",
        "description": "Activate one validated immutable transcript version.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "transcript_version_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "source_id",
                "transcript_version_id",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "transcript_versions_read",
        "description": "Read bounded transcript version metadata and active Edit reference status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
            },
            "required": ["project_path", "source_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "brief_create",
        "description": "Create and activate a minimal versioned Edit Brief.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "theme": {"type": "string", "minLength": 1},
                "target_duration_ticks": {"type": "integer", "minimum": 1},
                "focus": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "allow_reorder": {"type": "boolean"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "theme",
                "target_duration_ticks",
                "focus",
                "allow_reorder",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "brief_read",
        "description": "Read an immutable Edit Brief.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "brief_id": {"type": "string", "minLength": 1},
            },
            "required": ["project_path", "brief_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_context",
        "description": "Read a bounded, path-free Agent context page with a stable hash.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "transcript_version_id": {"type": "string", "minLength": 1},
                "brief_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": [
                "project_path",
                "source_id",
                "transcript_version_id",
                "brief_id",
                "expected_revision",
                "offset",
                "limit",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "multi_source_context",
        "description": "Read one bounded page across explicit ordered active source bindings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_bindings": {
                    "type": "array",
                    "minItems": 2,
                    "items": SOURCE_BINDING_SCHEMA,
                },
                "brief_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": [
                "project_path",
                "source_bindings",
                "brief_id",
                "expected_revision",
                "offset",
                "limit",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "revision_context",
        "description": "Read a bounded revision context from the current active Edit Decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["project_path", "expected_revision", "offset", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "name": "multi_source_proposal_create",
        "description": "Validate and store a schema-2 proposal over ordered source bindings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_bindings": {
                    "type": "array",
                    "minItems": 2,
                    "items": SOURCE_BINDING_SCHEMA,
                },
                "brief_id": {"type": "string", "minLength": 1},
                "context_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "clips": {"type": "array", "minItems": 1, "items": CLIP_SCHEMA},
                "total_duration_ticks": {"type": "integer", "minimum": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "source_bindings",
                "brief_id",
                "context_hash",
                "clips",
                "total_duration_ticks",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "multi_source_proposal_confirm",
        "description": "Explicitly confirm one current schema-2 proposal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "proposal_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "proposal_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "decision_read",
        "description": "Read either confirmed Edit Decision schema through one closed dispatcher.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "edit_version_id": {"type": "string", "minLength": 1},
            },
            "required": ["project_path", "edit_version_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "multi_source_edit_decision_read",
        "description": "Read an immutable confirmed schema-2 edit decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "edit_version_id": {"type": "string", "minLength": 1},
            },
            "required": ["project_path", "edit_version_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "proposal_create",
        "description": "Validate and store a complete ordered Edit Proposal snapshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "transcript_version_id": {"type": "string", "minLength": 1},
                "brief_id": {"type": "string", "minLength": 1},
                "context_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "clips": {"type": "array", "minItems": 1, "items": CLIP_SCHEMA},
                "total_duration_ticks": {"type": "integer", "minimum": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "source_id",
                "transcript_version_id",
                "brief_id",
                "context_hash",
                "clips",
                "total_duration_ticks",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "proposal_diff_read",
        "description": "Read a deterministic structural diff for a current revision Proposal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "proposal_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "proposal_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "proposal_confirm",
        "description": "Explicitly confirm one current proposal as an immutable Edit Decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "proposal_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "proposal_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "proposal_reject",
        "description": "Reject a proposal without changing the project or creating a decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "proposal_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "proposal_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_decision_read",
        "description": "Read an immutable confirmed Edit Decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "edit_version_id": {"type": "string", "minLength": 1},
            },
            "required": ["project_path", "edit_version_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_change",
        "description": "Apply one user-authorized delete, restore, reorder, or trim as an immutable Decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "base_edit_version_id": {"type": "string", "minLength": 1},
                "operation": EDIT_OPERATION_SCHEMA,
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "base_edit_version_id",
                "operation",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_undo",
        "description": "Move the active Edit pointer to its validated parent Decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "base_edit_version_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "base_edit_version_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_redo",
        "description": "Move the active Edit pointer to the next validated redo Decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "base_edit_version_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "base_edit_version_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_history_read",
        "description": "Read path-free active Edit ancestry, redo state, restorable clips, and current clips.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_path": {"type": "string", "minLength": 1}},
            "required": ["project_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "proxy_create",
        "description": "Create or reuse one verified project-local playback proxy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "operation_id": _WORKFLOW_ID_SCHEMA,
                "source_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": [
                "project_path",
                "operation_id",
                "source_id",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "proxy_read",
        "description": "Read and verify the current proxy cache state for one source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "source_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "render_roughcut",
        "description": "Render the current active Edit Decision to a verified H.264/AAC MP4.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "edit_version_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["project_path", "edit_version_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
)


def _response(request_id: object, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_result(
    payload: dict[str, object],
    *,
    is_error: bool = False,
    content_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    payload if content_payload is None else content_payload,
                    ensure_ascii=False,
                ),
            }
        ],
        "structuredContent": payload,
        "isError": is_error,
    }


def _is_notification(request: Mapping[str, Any]) -> bool:
    return (
        request.get("jsonrpc") == "2.0"
        and isinstance(request.get("method"), str)
        and "id" not in request
    )


def _same_request_id(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def handle_request(request: Mapping[str, Any]) -> dict[str, object] | None:
    """Handle one JSON-RPC request without writing to either standard stream."""
    if request.get("jsonrpc") != "2.0":
        return _error(request.get("id"), -32600, "Invalid Request")

    request_id = request.get("id")
    method = request.get("method")
    if not isinstance(method, str):
        return _error(request_id, -32600, "Invalid Request")
    if "id" not in request:
        return None
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _response(request_id, {})
    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "roughcut", "version": __version__},
            },
        )
    if method == "tools/list":
        return _response(
            request_id, {"tools": list(TOOLS), "toolSchemaVersion": TOOL_SCHEMA_VERSION}
        )
    if method != "tools/call":
        return _error(request_id, -32601, "Method not found")

    params = request.get("params")
    if not isinstance(params, Mapping):
        return _error(request_id, -32602, "Invalid params")
    tool_name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(tool_name, str) or not isinstance(arguments, Mapping):
        return _error(request_id, -32602, "Invalid params")
    if tool_name == "health":
        return _response(request_id, _tool_result(health()))
    if tool_name == "diagnostics":
        return _response(request_id, _tool_result(diagnostics()))
    if tool_name in {
        "qwen_credential_configure",
        "qwen_credential_readiness",
        "qwen_credential_clear",
    }:
        try:
            if tool_name == "qwen_credential_configure":
                _reject_unexpected_arguments(arguments, "api_key", "workspace_id")
                credential = configure_qwen_credential(
                    api_key=_string_argument(arguments, "api_key"),
                    workspace_id=_string_argument(arguments, "workspace_id"),
                )
            elif tool_name == "qwen_credential_clear":
                _reject_unexpected_arguments(arguments)
                credential = clear_qwen_credential()
            else:
                _reject_unexpected_arguments(arguments)
                credential = qwen_credential_readiness()
        except QwenCredentialError as error:
            return _response(
                request_id,
                _tool_result(
                    _versioned_error(public_credential_error_code(error)),
                    is_error=True,
                ),
            )
        except ValueError:
            return _response(
                request_id,
                _tool_result(_versioned_error("invalid_arguments"), is_error=True),
            )
        payload = health()
        payload["credential"] = credential
        return _response(request_id, _tool_result(payload))
    if tool_name == "media_operation_status":
        try:
            _reject_unexpected_arguments(
                arguments, "project_path", "operation_id"
            )
            operation = media_operation_status(
                Path(_string_argument(arguments, "project_path")),
                _string_argument(arguments, "operation_id"),
            )
            return _response(
                request_id,
                _tool_result(_media_operation_status_payload(operation)),
            )
        except MediaOperationError as error:
            return _response(
                request_id,
                _tool_result(_versioned_error(error.code), is_error=True),
            )
        except ValueError:
            return _response(
                request_id,
                _tool_result(
                    _versioned_error("invalid_arguments"),
                    is_error=True,
                ),
            )
        except (OSError, ProjectError):
            return _response(
                request_id,
                _tool_result(
                    _versioned_error("operation_integrity_error"),
                    is_error=True,
                ),
            )
    if tool_name == "approve_nle_export":
        try:
            _reject_unexpected_arguments(
                arguments,
                "project_path", "run_id", "action_id", "edit_version_id",
                "expected_revision", "route", "destination", "alignment_artifact_id",
            )
            if "alignment_artifact_id" not in arguments:
                raise ValueError("alignment_artifact_id is required")
            alignment_artifact_id = arguments.get("alignment_artifact_id")
            if alignment_artifact_id is not None and not isinstance(alignment_artifact_id, str):
                raise ValueError("alignment_artifact_id must be a string or null")
            outcome = approve_nle_export(
                Path(_string_argument(arguments, "project_path")),
                run_id=_string_argument(arguments, "run_id"),
                action_id=_string_argument(arguments, "action_id"),
                edit_version_id=_string_argument(arguments, "edit_version_id"),
                expected_revision=_integer_argument(arguments, "expected_revision"),
                route=_string_argument(arguments, "route"),
                destination=_string_argument(arguments, "destination"),
                alignment_artifact_id=alignment_artifact_id,
            )
            payload = health()
            payload.update(outcome.to_dict())
            return _response(request_id, _tool_result(payload))
        except NleHandoffError as error:
            return _response(request_id, _tool_result(_versioned_error(error.code), is_error=True))
        except (OSError, ProjectError):
            return _response(
                request_id,
                _tool_result(_versioned_error("nle_export_integrity_error"), is_error=True),
            )
        except ValueError:
            return _response(
                request_id,
                _tool_result(_versioned_error("invalid_arguments"), is_error=True),
            )
    if tool_name in {
        "align_multicam",
        "multicam_parallel_render_prepare",
        "multicam_parallel_render_start",
    }:
        try:
            if tool_name == "align_multicam":
                _reject_unexpected_arguments(
                    arguments,
                    "project_path", "operation_id", "alignment_id", "expected_revision",
                    "main_camera", "auxiliary_cameras", "main_audio_stable",
                    "max_temporary_disk_bytes", "max_analysis_memory_bytes", "max_runtime_seconds",
                )
                project_path = _string_argument(arguments, "project_path")
                operation_id = _string_argument(arguments, "operation_id")
                alignment_id = _string_argument(arguments, "alignment_id")
                expected_revision = _integer_argument(arguments, "expected_revision")
                main_camera = _object_argument(arguments, "main_camera")
                auxiliary_cameras = _object_list_argument(arguments, "auxiliary_cameras")
                main_audio_stable = _boolean_argument(arguments, "main_audio_stable")
                max_temporary_disk_bytes = _integer_argument(
                    arguments, "max_temporary_disk_bytes"
                )
                max_analysis_memory_bytes = _integer_argument(
                    arguments, "max_analysis_memory_bytes"
                )
                max_runtime_seconds = _integer_argument(arguments, "max_runtime_seconds")
                require_m2_7_public_capability("align_multicam")
                alignment_outcome = run_align_multicam(
                    Path(project_path),
                    operation_id=operation_id,
                    alignment_id=alignment_id,
                    expected_revision=expected_revision,
                    main_camera=main_camera,
                    auxiliary_cameras=auxiliary_cameras,
                    main_audio_stable=main_audio_stable,
                    max_temporary_disk_bytes=max_temporary_disk_bytes,
                    max_analysis_memory_bytes=max_analysis_memory_bytes,
                    max_runtime_seconds=max_runtime_seconds,
                )
                payload = _alignment_operation_payload(alignment_outcome)
            elif tool_name == "multicam_parallel_render_prepare":
                _reject_unexpected_arguments(
                    arguments,
                    "project_path", "edit_version_id", "alignment_ref",
                    "auxiliary_camera_ids", "expected_revision",
                )
                project_path = _string_argument(arguments, "project_path")
                edit_version_id = _string_argument(arguments, "edit_version_id")
                alignment_ref = _object_argument(arguments, "alignment_ref")
                auxiliary_camera_ids = _string_array_argument(
                    arguments, "auxiliary_camera_ids"
                )
                expected_revision = _integer_argument(arguments, "expected_revision")
                require_m2_7_public_capability("multicam_parallel_render_prepare")
                parallel_prepare_outcome = prepare_multicam_parallel_render(
                    Path(project_path),
                    edit_version_id=edit_version_id,
                    alignment_ref=alignment_ref,
                    auxiliary_camera_ids=auxiliary_camera_ids,
                    expected_revision=expected_revision,
                )
                payload = _parallel_prepare_payload(parallel_prepare_outcome)
            else:
                _reject_unexpected_arguments(arguments, "project_path", "operation_id", "prepare_ref")
                project_path = _string_argument(arguments, "project_path")
                operation_id = _string_argument(arguments, "operation_id")
                prepare_ref = _object_argument(arguments, "prepare_ref")
                require_m2_7_public_capability("multicam_parallel_render_start")
                parallel_render_outcome = start_multicam_parallel_render(
                    Path(project_path),
                    operation_id=operation_id,
                    prepare_ref=prepare_ref,
                )
                payload = _parallel_operation_payload(parallel_render_outcome)
            return _response(request_id, _tool_result(payload))
        except (AlignmentError, MediaOperationError, ParallelRenderError) as error:
            return _response(
                request_id,
                _tool_result(_versioned_error(error.code), is_error=True),
            )
        except (OSError, ProjectError):
            return _response(
                request_id,
                _tool_result(_versioned_error("operation_integrity_error"), is_error=True),
            )
        except ValueError:
            return _response(
                request_id,
                _tool_result(_versioned_error("invalid_arguments"), is_error=True),
            )
    if tool_name in {
        "workflow_start",
        "workflow_status",
        "workflow_action",
        "workflow_cancel",
    }:
        try:
            if tool_name == "workflow_start":
                _reject_unexpected_arguments(
                    arguments,
                    "project_path",
                    "run_id",
                    "ordered_source_ids",
                )
                workflow_result = workflow_start(
                    Path(_string_argument(arguments, "project_path")),
                    _string_argument(arguments, "run_id"),
                    _string_array_argument(arguments, "ordered_source_ids"),
                )
                payload = _workflow_result_payload(workflow_result)
            elif tool_name == "workflow_status":
                _reject_unexpected_arguments(
                    arguments, "project_path", "run_id"
                )
                run_id = _optional_string_argument(arguments, "run_id")
                status = workflow_status(
                    Path(_string_argument(arguments, "project_path")), run_id
                )
                payload = _workflow_status_payload(status)
            elif tool_name == "workflow_action":
                _reject_unexpected_arguments(
                    arguments,
                    "project_path",
                    "run_id",
                    "action_id",
                    "action",
                    "input",
                )
                action = _string_argument(arguments, "action")
                if action == "approve_export":
                    export_project_path = Path(
                        _string_argument(arguments, "project_path")
                    )
                    export_run_id = _string_argument(arguments, "run_id")
                    export_action_id = _string_argument(arguments, "action_id")
                    export_action_input = _object_argument(arguments, "input")
                    try:
                        export_outcome = _call_with_exception_boundary(
                            lambda: run_approve_export_operation(
                                export_project_path,
                                run_id=export_run_id,
                                action_id=export_action_id,
                                action_input=export_action_input,
                            ),
                            passthrough=(
                                WorkflowError,
                                MediaOperationError,
                                RenderCancelled,
                            ),
                        )
                    except (WorkflowError, MediaOperationError) as error:
                        return _response(
                            request_id,
                            _error_tool_result(error),
                        )
                    except RenderCancelled:
                        return _response(
                            request_id,
                            _tool_result(
                                _versioned_error("render_cancelled"),
                                is_error=True,
                            ),
                        )
                    except _UnexpectedOperationError:
                        return _response(
                            request_id,
                            _tool_result(
                                _versioned_error("render_operation_failed"),
                                is_error=True,
                            ),
                        )
                    payload = _approve_export_operation_payload(export_outcome)
                else:
                    workflow_result = workflow_action(
                        Path(_string_argument(arguments, "project_path")),
                        _string_argument(arguments, "run_id"),
                        _string_argument(arguments, "action_id"),
                        action,
                        _object_argument(arguments, "input"),
                    )
                    payload = _workflow_result_payload(workflow_result)
            else:
                _reject_unexpected_arguments(
                    arguments, "project_path", "run_id", "action_id"
                )
                workflow_result = workflow_cancel(
                    Path(_string_argument(arguments, "project_path")),
                    _string_argument(arguments, "run_id"),
                    _string_argument(arguments, "action_id"),
                )
                payload = _workflow_result_payload(workflow_result)
            return _response(request_id, _tool_result(payload))
        except (WorkflowError, MediaOperationError) as error:
            return _response(
                request_id,
                _error_tool_result(error),
            )
        except (OSError, ValueError):
            return _response(
                request_id,
                _tool_result(
                    _versioned_error("invalid_arguments"), is_error=True
                ),
            )
    if tool_name == "fake_project_roundtrip":
        project_name = arguments.get("project_name")
        if not isinstance(project_name, str):
            return _response(
                request_id,
                _tool_result({"ok": False, "error": {"code": "invalid_input"}}, is_error=True),
            )
        try:
            return _response(request_id, _tool_result(fake_project_roundtrip(project_name)))
        except ValueError:
            return _response(
                request_id,
                _tool_result({"ok": False, "error": {"code": "invalid_input"}}, is_error=True),
            )
    try:
        if tool_name == "person_create":
            result = create_person(
                Path(_string_argument(arguments, "project_path")),
                name=_string_argument(arguments, "name"),
                role=_string_argument(arguments, "role"),
                note=_string_argument(arguments, "note", allow_empty=True),
                expected_revision=_integer_argument(arguments, "expected_revision"),
            )
            return _response(request_id, _tool_result(_people_mutation_payload(result)))
        if tool_name == "source_metadata_update":
            display_name = arguments.get("display_name")
            if display_name is not None and not isinstance(display_name, str):
                raise ValueError("display_name must be a string")
            result = update_source_metadata(
                Path(_string_argument(arguments, "project_path")),
                source_id=_string_argument(arguments, "source_id"),
                display_name=display_name,
                tags=_string_array_argument(arguments, "tags"),
                note=_string_argument(arguments, "note", allow_empty=True),
                expected_revision=_integer_argument(arguments, "expected_revision"),
            )
            return _response(request_id, _tool_result(_people_mutation_payload(result)))
        if tool_name == "speaker_map_confirm":
            result = confirm_speaker_map(
                Path(_string_argument(arguments, "project_path")),
                source_id=_string_argument(arguments, "source_id"),
                transcript_version_id=_string_argument(arguments, "transcript_version_id"),
                local_speaker_id=_string_argument(arguments, "local_speaker_id"),
                person_id=_string_argument(arguments, "person_id"),
                confirmed_by_user=_boolean_argument(arguments, "confirmed_by_user"),
                expected_revision=_integer_argument(arguments, "expected_revision"),
            )
            return _response(request_id, _tool_result(_people_mutation_payload(result)))
        if tool_name == "people_read":
            people_state = read_people(Path(_string_argument(arguments, "project_path")))
            return _response(request_id, _tool_result(_people_state_payload(people_state)))
    except (OSError, ProjectError, ValueError):
        return _response(
            request_id,
            _tool_result(_versioned_error("people_operation_failed"), is_error=True),
        )
    if tool_name in {"revision_context", "proposal_diff_read"}:
        try:
            revision_project = Path(_string_argument(arguments, "project_path"))
            revision_expected = _integer_argument(arguments, "expected_revision")
            revision_offset = (
                _integer_argument(arguments, "offset") if tool_name == "revision_context" else None
            )
            revision_limit = (
                _integer_argument(arguments, "limit") if tool_name == "revision_context" else None
            )
            revision_proposal_id = (
                _string_argument(arguments, "proposal_id")
                if tool_name == "proposal_diff_read"
                else None
            )
        except ValueError:
            return _response(
                request_id,
                _tool_result(_versioned_error("invalid_arguments"), is_error=True),
            )
        try:
            if tool_name == "revision_context":
                assert revision_offset is not None
                assert revision_limit is not None
                revision_page = read_revision_context(
                    revision_project,
                    expected_revision=revision_expected,
                    offset=revision_offset,
                    limit=revision_limit,
                )
                payload = health()
                payload["revision_context"] = revision_page.to_dict()
                return _response(request_id, _tool_result(payload))
            assert revision_proposal_id is not None
            proposal_diff = read_proposal_diff(
                revision_project,
                revision_proposal_id,
                expected_revision=revision_expected,
            )
            return _response(
                request_id,
                _tool_result(_proposal_diff_payload(proposal_diff)),
            )
        except (OSError, ProjectError):
            error_code = (
                "revision_context_failed"
                if tool_name == "revision_context"
                else "proposal_diff_failed"
            )
            return _response(
                request_id,
                _tool_result(_versioned_error(error_code), is_error=True),
            )
    try:
        if tool_name == "decision_read":
            decision_read_state = read_decision(
                Path(_string_argument(arguments, "project_path")),
                _string_argument(arguments, "edit_version_id"),
            )
            return _response(
                request_id,
                _tool_result(_decision_read_payload(decision_read_state)),
            )
    except WorkflowError as error:
        return _response(
            request_id,
            _error_tool_result(error),
        )
    except (OSError, ProjectError, ValueError):
        return _response(
            request_id,
            _tool_result(_versioned_error("decision_read_integrity"), is_error=True),
        )
    try:
        if tool_name == "multi_source_context":
            multi_context = read_multi_source_agent_context(
                Path(_string_argument(arguments, "project_path")),
                source_bindings=_object_list_argument(arguments, "source_bindings"),
                brief_id=_string_argument(arguments, "brief_id"),
                expected_revision=_integer_argument(arguments, "expected_revision"),
                offset=_integer_argument(arguments, "offset"),
                limit=_integer_argument(arguments, "limit"),
            )
            payload = health()
            payload["multi_source_context"] = multi_context.to_dict()
            return _response(request_id, _tool_result(payload))
        if tool_name == "multi_source_proposal_create":
            guarded_project_path = Path(_string_argument(arguments, "project_path"))
            with protected_write(guarded_project_path, operation=tool_name):
                multi_proposal_state = create_multi_source_edit_proposal(
                    guarded_project_path,
                    source_bindings=_object_list_argument(arguments, "source_bindings"),
                    brief_id=_string_argument(arguments, "brief_id"),
                    context_hash=_string_argument(arguments, "context_hash"),
                    clips=_object_list_argument(arguments, "clips"),
                    total_duration_ticks=_integer_argument(arguments, "total_duration_ticks"),
                    expected_revision=_integer_argument(arguments, "expected_revision"),
                )
            return _response(
                request_id,
                _tool_result(_multi_source_proposal_payload(multi_proposal_state)),
            )
        if tool_name == "multi_source_proposal_confirm":
            guarded_project_path = Path(_string_argument(arguments, "project_path"))
            with protected_write(guarded_project_path, operation=tool_name):
                multi_decision_state = confirm_multi_source_edit_proposal(
                    guarded_project_path,
                    _string_argument(arguments, "proposal_id"),
                    expected_revision=_integer_argument(arguments, "expected_revision"),
                )
            return _response(
                request_id,
                _tool_result(_multi_source_decision_payload(multi_decision_state)),
            )
        if tool_name == "multi_source_edit_decision_read":
            multi_read_state = read_multi_source_edit_decision(
                Path(_string_argument(arguments, "project_path")),
                _string_argument(arguments, "edit_version_id"),
            )
            return _response(
                request_id,
                _tool_result(_multi_source_decision_payload(multi_read_state)),
            )
    except WorkflowError as error:
        return _response(
            request_id,
            _error_tool_result(error),
        )
    except (OSError, ProjectError, ValueError):
        return _response(
            request_id,
            _tool_result(_versioned_error("multi_source_operation_failed"), is_error=True),
        )
    try:
        if tool_name == "brief_create":
            guarded_project_path = Path(_string_argument(arguments, "project_path"))
            with protected_write(guarded_project_path, operation=tool_name):
                state = create_edit_brief(
                    guarded_project_path,
                    theme=_string_argument(arguments, "theme"),
                    target_duration_ticks=_integer_argument(arguments, "target_duration_ticks"),
                    focus=_string_list_argument(arguments, "focus"),
                    allow_reorder=_boolean_argument(arguments, "allow_reorder"),
                    expected_revision=_integer_argument(arguments, "expected_revision"),
                )
            return _response(request_id, _tool_result(_brief_payload(state)))
        if tool_name == "brief_read":
            state = read_edit_brief(
                Path(_string_argument(arguments, "project_path")),
                _string_argument(arguments, "brief_id"),
            )
            return _response(request_id, _tool_result(_brief_payload(state)))
        if tool_name == "agent_context":
            context = read_agent_context(
                Path(_string_argument(arguments, "project_path")),
                source_id=_string_argument(arguments, "source_id"),
                transcript_version_id=_string_argument(arguments, "transcript_version_id"),
                brief_id=_string_argument(arguments, "brief_id"),
                expected_revision=_integer_argument(arguments, "expected_revision"),
                offset=_integer_argument(arguments, "offset"),
                limit=_integer_argument(arguments, "limit"),
            )
            payload = health()
            payload["agent_context"] = context.to_dict()
            return _response(request_id, _tool_result(payload))
    except WorkflowError as error:
        return _response(
            request_id,
            _error_tool_result(error),
        )
    except (OSError, ProjectError, ValueError):
        return _response(
            request_id,
            _tool_result(_versioned_error("agent_operation_failed"), is_error=True),
        )
    try:
        if tool_name == "proposal_create":
            guarded_project_path = Path(_string_argument(arguments, "project_path"))
            with protected_write(guarded_project_path, operation=tool_name):
                proposal_state = create_edit_proposal(
                    guarded_project_path,
                    source_id=_string_argument(arguments, "source_id"),
                    transcript_version_id=_string_argument(arguments, "transcript_version_id"),
                    brief_id=_string_argument(arguments, "brief_id"),
                    context_hash=_string_argument(arguments, "context_hash"),
                    clips=_object_list_argument(arguments, "clips"),
                    total_duration_ticks=_integer_argument(arguments, "total_duration_ticks"),
                    expected_revision=_integer_argument(arguments, "expected_revision"),
                )
            return _response(request_id, _tool_result(_proposal_payload(proposal_state)))
        if tool_name == "proposal_confirm":
            guarded_project_path = Path(_string_argument(arguments, "project_path"))
            with protected_write(guarded_project_path, operation=tool_name):
                decision_state = confirm_edit_proposal(
                    guarded_project_path,
                    _string_argument(arguments, "proposal_id"),
                    expected_revision=_integer_argument(arguments, "expected_revision"),
                )
            return _response(request_id, _tool_result(_decision_payload(decision_state)))
        if tool_name == "proposal_reject":
            guarded_project_path = Path(_string_argument(arguments, "project_path"))
            proposal_id = _string_argument(arguments, "proposal_id")
            with protected_write(
                guarded_project_path,
                operation=tool_name,
                proposal_id=proposal_id,
            ) as run:
                current_proposal = run.artifact_refs["proposal"]
                assert current_proposal is not None
                reject = (
                    reject_multi_source_edit_proposal
                    if current_proposal.schema_version == 2
                    else reject_edit_proposal
                )
                rejection = reject(
                    guarded_project_path,
                    proposal_id,
                    expected_revision=_integer_argument(arguments, "expected_revision"),
                )
            payload = health()
            payload["proposal_rejection"] = rejection.to_dict()
            return _response(request_id, _tool_result(payload))
        if tool_name == "edit_decision_read":
            read_state = read_edit_decision(
                Path(_string_argument(arguments, "project_path")),
                _string_argument(arguments, "edit_version_id"),
            )
            return _response(request_id, _tool_result(_decision_payload(read_state)))
    except WorkflowError as error:
        return _response(
            request_id,
            _error_tool_result(error),
        )
    except (OSError, ProjectError, ValueError):
        return _response(
            request_id,
            _tool_result(_versioned_error("proposal_operation_failed"), is_error=True),
        )
    if tool_name in {"edit_change", "edit_undo", "edit_redo", "edit_history_read"}:
        try:
            edit_project_path = Path(_string_argument(arguments, "project_path"))
            edit_base_version_id = (
                _string_argument(arguments, "base_edit_version_id")
                if tool_name != "edit_history_read"
                else None
            )
            edit_expected_revision = (
                _integer_argument(arguments, "expected_revision")
                if tool_name != "edit_history_read"
                else None
            )
            edit_operation = (
                _object_argument(arguments, "operation") if tool_name == "edit_change" else None
            )
        except ValueError:
            return _response(
                request_id,
                _tool_result(_versioned_error("invalid_arguments"), is_error=True),
            )
        try:
            if tool_name == "edit_change":
                assert edit_operation is not None
                assert edit_expected_revision is not None
                assert edit_base_version_id is not None
                changed = change_edit(
                    edit_project_path,
                    operation=edit_operation,
                    expected_revision=edit_expected_revision,
                    base_edit_version_id=edit_base_version_id,
                )
                return _response(request_id, _tool_result(_edit_change_payload(changed)))
            if tool_name in {"edit_undo", "edit_redo"}:
                assert edit_expected_revision is not None
                assert edit_base_version_id is not None
                navigation = (undo_edit if tool_name == "edit_undo" else redo_edit)(
                    edit_project_path,
                    expected_revision=edit_expected_revision,
                    base_edit_version_id=edit_base_version_id,
                )
                return _response(
                    request_id,
                    _tool_result(_edit_navigation_payload(navigation)),
                )
            history = read_edit_history(edit_project_path)
            return _response(request_id, _tool_result(_edit_history_payload(history)))
        except (OSError, ProjectError):
            return _response(
                request_id,
                _tool_result(_versioned_error("edit_operation_failed"), is_error=True),
            )
    if tool_name in {"proxy_create", "proxy_read"}:
        proxy_operation_id: str | None = None
        try:
            if tool_name == "proxy_create":
                _reject_unexpected_arguments(
                    arguments,
                    "project_path",
                    "operation_id",
                    "source_id",
                    "expected_revision",
                )
                proxy_operation_id = _string_argument(arguments, "operation_id")
            else:
                _reject_unexpected_arguments(
                    arguments,
                    "project_path",
                    "source_id",
                    "expected_revision",
                )
            proxy_project_path = Path(_string_argument(arguments, "project_path"))
            proxy_source_id = _string_argument(arguments, "source_id")
            proxy_expected_revision = _integer_argument(arguments, "expected_revision")
        except ValueError:
            return _response(
                request_id,
                _tool_result(_versioned_error("invalid_arguments"), is_error=True),
            )
        try:
            if tool_name == "proxy_create":
                assert proxy_expected_revision is not None
                assert proxy_operation_id is not None
                try:
                    created = _call_with_exception_boundary(
                        lambda: run_proxy_operation(
                            proxy_project_path,
                            operation_id=proxy_operation_id,
                            source_id=proxy_source_id,
                            expected_project_revision=proxy_expected_revision,
                        ),
                        passthrough=(
                            WorkflowError,
                            MediaOperationError,
                            ProxyCancelled,
                            ProxyUnsupported,
                        ),
                    )
                except (WorkflowError, MediaOperationError) as error:
                    return _response(
                        request_id,
                        _error_tool_result(error),
                    )
                except ProxyCancelled:
                    return _response(
                        request_id,
                        _tool_result(
                            _versioned_error("proxy_cancelled"),
                            is_error=True,
                        ),
                    )
                except ProxyUnsupported:
                    return _response(
                        request_id,
                        _tool_result(
                            _versioned_error("proxy_unsupported"),
                            is_error=True,
                        ),
                    )
                except _UnexpectedOperationError:
                    return _response(
                        request_id,
                        _tool_result(
                            _versioned_error("proxy_operation_failed"),
                            is_error=True,
                        ),
                    )
                return _response(
                    request_id,
                    _tool_result(_proxy_operation_payload(created)),
                )
            proxy_state = read_proxy(
                proxy_project_path,
                source_id=proxy_source_id,
                expected_revision=proxy_expected_revision,
            )
            return _response(request_id, _tool_result(_proxy_payload(proxy_state)))
        except MediaOperationError as error:
            return _response(
                request_id,
                _tool_result(_versioned_error(error.code), is_error=True),
            )
        except (KeyboardInterrupt, ProxyCancelled):
            return _response(
                request_id,
                _tool_result(_versioned_error("proxy_cancelled"), is_error=True),
            )
        except ProxyUnsupported:
            return _response(
                request_id,
                _tool_result(_versioned_error("proxy_unsupported"), is_error=True),
            )
        except (FFmpegProxyError, OSError, ProjectError):
            return _response(
                request_id,
                _tool_result(_versioned_error("proxy_operation_failed"), is_error=True),
            )
    try:
        if tool_name == "render_roughcut":
            guarded_project_path = Path(_string_argument(arguments, "project_path"))
            with protected_write(guarded_project_path, operation=tool_name):
                rendered = render_roughcut(
                    guarded_project_path,
                    edit_version_id=_string_argument(arguments, "edit_version_id"),
                    expected_revision=_integer_argument(arguments, "expected_revision"),
                )
            return _response(request_id, _tool_result(_render_payload(rendered)))
    except WorkflowError as error:
        return _response(
            request_id,
            _error_tool_result(error),
        )
    except RenderCancelled:
        return _response(
            request_id,
            _tool_result(_versioned_error("render_cancelled"), is_error=True),
        )
    except (FFmpegRenderError, RenderVerificationError, OSError, ProjectError, ValueError):
        return _response(
            request_id,
            _tool_result(_versioned_error("render_operation_failed"), is_error=True),
        )
    try:
        if tool_name == "readable_transcript_read":
            readable_page = read_readable_transcript(
                Path(_string_argument(arguments, "project_path")),
                source_bindings=_object_list_argument(arguments, "source_bindings"),
                expected_revision=_integer_argument(arguments, "expected_revision"),
                offset=_integer_argument(arguments, "offset"),
                limit=_integer_argument(arguments, "limit"),
                filters=_optional_object_argument(arguments, "filters"),
                overlay=_optional_object_argument(arguments, "overlay"),
            )
            payload = health()
            payload["readable_transcript"] = readable_page.to_dict()
            return _response(request_id, _tool_result(payload))
        if tool_name == "transcript_selection_resolve":
            selection = resolve_transcript_selection(
                Path(_string_argument(arguments, "project_path")),
                source_bindings=_object_list_argument(arguments, "source_bindings"),
                expected_revision=_integer_argument(arguments, "expected_revision"),
                view_hash=_string_argument(arguments, "view_hash"),
                selections=_object_list_argument(arguments, "selections"),
                overlay=_optional_object_argument(arguments, "overlay"),
            )
            payload = health()
            payload["transcript_selection"] = selection.to_dict()
            return _response(request_id, _tool_result(payload))
        if tool_name == "markdown_export":
            exported = export_markdown(
                Path(_string_argument(arguments, "project_path")),
                basis=_string_argument(arguments, "basis"),
                output_path=Path(_string_argument(arguments, "output_path")),
                expected_revision=_integer_argument(arguments, "expected_revision"),
                source_bindings=_optional_object_list_argument(arguments, "source_bindings"),
                artifact_id=_optional_string_argument(arguments, "artifact_id"),
            )
            payload = health()
            payload["markdown_export"] = exported.to_dict()
            return _response(request_id, _tool_result(payload))
    except (OSError, ProjectError, ValueError):
        error_codes = {
            "readable_transcript_read": "readable_transcript_failed",
            "transcript_selection_resolve": "transcript_selection_failed",
            "markdown_export": "markdown_export_failed",
        }
        return _response(
            request_id,
            _tool_result(
                _versioned_error(error_codes.get(tool_name, "readable_transcript_failed")),
                is_error=True,
            ),
        )
    if tool_name in {
        "content_draft_create",
        "content_draft_revise_scoped",
        "content_draft_read",
        "content_draft_confirm",
        "content_draft_propose",
    }:
        try:
            draft_project_path = Path(_string_argument(arguments, "project_path"))
            draft_id = (
                _string_argument(arguments, "content_draft_id")
                if tool_name
                not in {"content_draft_create", "content_draft_revise_scoped"}
                else None
            )
            draft_expected_revision = (
                _integer_argument(arguments, "expected_revision")
                if tool_name not in {"content_draft_read"}
                else None
            )
            draft_parent_id = (
                _optional_string_argument(arguments, "parent_draft_id")
                if tool_name
                in {"content_draft_create", "content_draft_revise_scoped"}
                else None
            )
            draft_display_title = (
                _optional_string_argument(arguments, "display_title")
                if tool_name == "content_draft_create"
                else None
            )
            draft_bindings = (
                _object_list_argument(arguments, "source_bindings")
                if tool_name == "content_draft_create"
                else None
            )
            draft_brief_id = (
                _string_argument(arguments, "brief_id")
                if tool_name == "content_draft_create"
                else None
            )
            draft_context_hash = (
                _string_argument(arguments, "context_hash")
                if tool_name == "content_draft_create"
                else None
            )
            draft_blocks = (
                _object_list_argument(arguments, "blocks")
                if tool_name
                in {"content_draft_create", "content_draft_revise_scoped"}
                else None
            )
            mutable_block_ids = (
                _string_list_argument(arguments, "mutable_block_ids")
                if tool_name == "content_draft_revise_scoped"
                else None
            )
        except ValueError:
            return _response(
                request_id,
                _tool_result(_versioned_error("invalid_arguments"), is_error=True),
            )
        try:
            if tool_name == "content_draft_create":
                assert draft_bindings is not None
                assert draft_brief_id is not None
                assert draft_context_hash is not None
                assert draft_blocks is not None
                assert draft_expected_revision is not None
                with protected_write(draft_project_path, operation=tool_name):
                    draft_payload = create_content_draft(
                        draft_project_path,
                        parent_draft_id=draft_parent_id,
                        display_title=draft_display_title,
                        source_bindings=draft_bindings,
                        brief_id=draft_brief_id,
                        context_hash=draft_context_hash,
                        blocks=draft_blocks,
                        expected_revision=draft_expected_revision,
                    ).to_dict()
            elif tool_name == "content_draft_revise_scoped":
                assert draft_parent_id is not None
                assert mutable_block_ids is not None
                assert draft_blocks is not None
                assert draft_expected_revision is not None
                with protected_write(draft_project_path, operation=tool_name):
                    draft_payload = revise_content_draft_scoped(
                        draft_project_path,
                        parent_draft_id=draft_parent_id,
                        mutable_block_ids=mutable_block_ids,
                        blocks=draft_blocks,
                        expected_revision=draft_expected_revision,
                    ).to_dict()
            elif tool_name == "content_draft_read":
                assert draft_id is not None
                draft_payload = read_content_draft(draft_project_path, draft_id).to_dict()
            elif tool_name == "content_draft_confirm":
                assert draft_id is not None
                assert draft_expected_revision is not None
                with protected_write(draft_project_path, operation=tool_name):
                    draft_payload = confirm_content_draft(
                        draft_project_path,
                        draft_id,
                        expected_revision=draft_expected_revision,
                    ).to_dict()
            else:
                assert draft_id is not None
                assert draft_expected_revision is not None
                with protected_write(draft_project_path, operation=tool_name):
                    draft_payload = propose_content_draft(
                        draft_project_path,
                        draft_id,
                        expected_revision=draft_expected_revision,
                    ).to_dict()
            payload = health()
            payload.update(draft_payload)
            return _response(request_id, _tool_result(payload))
        except WorkflowError as error:
            return _response(
                request_id,
                _error_tool_result(error),
            )
        except (OSError, ProjectError, ValueError):
            return _response(
                request_id,
                _tool_result(
                    _versioned_error("content_draft_operation_failed"),
                    is_error=True,
                ),
            )
    try:
        if tool_name == "transcript_correct":
            mutation = correct_transcript(
                Path(_string_argument(arguments, "project_path")),
                source_id=_string_argument(arguments, "source_id"),
                parent_transcript_version_id=_string_argument(
                    arguments, "parent_transcript_version_id"
                ),
                corrections=_object_list_argument(arguments, "corrections"),
                expected_revision=_integer_argument(arguments, "expected_revision"),
            )
            return _response(
                request_id,
                _tool_result(_transcript_mutation_payload(mutation)),
            )
        if tool_name == "transcript_version_activate":
            activation = activate_transcript_version(
                Path(_string_argument(arguments, "project_path")),
                source_id=_string_argument(arguments, "source_id"),
                transcript_version_id=_string_argument(arguments, "transcript_version_id"),
                expected_revision=_integer_argument(arguments, "expected_revision"),
            )
            return _response(
                request_id,
                _tool_result(_transcript_activation_payload(activation)),
            )
        if tool_name == "transcript_versions_read":
            versions = read_transcript_versions(
                Path(_string_argument(arguments, "project_path")),
                source_id=_string_argument(arguments, "source_id"),
            )
            return _response(
                request_id,
                _tool_result(_transcript_versions_payload(versions)),
            )
    except WorkflowError as error:
        return _response(
            request_id,
            _error_tool_result(error),
        )
    except (OSError, ProjectError):
        return _response(
            request_id,
            _tool_result(_versioned_error("transcript_version_operation_failed"), is_error=True),
        )
    except ValueError:
        return _response(
            request_id,
            _tool_result(_versioned_error("invalid_input"), is_error=True),
        )
    try:
        if tool_name == "transcribe_source":
            _reject_unexpected_arguments(
                arguments,
                "project_path",
                "operation_id",
                "source_id",
                "expected_revision",
                "speaker_diarization",
            )
            transcription_project_path = Path(
                _string_argument(arguments, "project_path")
            )
            transcription_operation_id = _string_argument(
                arguments, "operation_id"
            )
            transcription_source_id = _string_argument(arguments, "source_id")
            transcription_expected_revision = _integer_argument(
                arguments, "expected_revision"
            )
            transcription_speaker_diarization = _optional_boolean_argument(
                arguments, "speaker_diarization", default=False
            )
            try:
                transcription_outcome = _call_with_exception_boundary(
                    lambda: run_transcription_operation(
                        transcription_project_path,
                        operation_id=transcription_operation_id,
                        source_id=transcription_source_id,
                        expected_project_revision=transcription_expected_revision,
                        speaker_diarization=transcription_speaker_diarization,
                    ),
                    passthrough=(WorkflowError, MediaOperationError),
                )
            except (WorkflowError, MediaOperationError) as error:
                return _response(
                    request_id,
                    _error_tool_result(error),
                )
            except _UnexpectedOperationError:
                return _response(
                    request_id,
                    _tool_result(
                        _versioned_error("transcription_failed"),
                        is_error=True,
                    ),
                )
            return _response(
                request_id,
                _tool_result(
                    _transcription_operation_payload(transcription_outcome)
                ),
            )
        if tool_name == "transcript_page":
            project_path = _string_argument(arguments, "project_path")
            source_id = _string_argument(arguments, "source_id")
            transcript_id = _string_argument(arguments, "transcript_version_id")
            page = read_transcript_page(
                Path(project_path),
                source_id,
                transcript_id,
                offset=_integer_argument(arguments, "offset"),
                limit=_integer_argument(arguments, "limit"),
            )
            payload = health()
            payload["transcript_page"] = page.to_dict()
            return _response(request_id, _tool_result(payload))
    except WorkflowError as error:
        return _response(
            request_id,
            _error_tool_result(error),
        )
    except MediaOperationError as error:
        return _response(
            request_id,
            _tool_result(_versioned_error(error.code), is_error=True),
        )
    except TranscriptNormalizationError:
        return _response(
            request_id,
            _tool_result(
                _versioned_error("transcription_failed"),
                is_error=True,
            ),
        )
    except (FunASRRunnerError, OSError, ProjectError):
        return _response(
            request_id,
            _tool_result(_versioned_error("transcription_failed"), is_error=True),
        )
    except ValueError:
        return _response(
            request_id,
            _tool_result(_versioned_error("invalid_arguments"), is_error=True),
        )
    try:
        if tool_name == "project_create":
            _reject_unexpected_arguments(
                arguments,
                "project_path",
                "name",
                "output_preset",
            )
            project_path = _string_argument(arguments, "project_path")
            name = _string_argument(arguments, "name")
            return _response(
                request_id,
                _tool_result(
                    _project_payload(
                        create_project(
                            Path(project_path),
                            name,
                            output_preset=_optional_string_argument(arguments, "output_preset"),
                        )
                    )
                ),
            )
        if tool_name == "project_open":
            project_path = _string_argument(arguments, "project_path")
            return _response(
                request_id, _tool_result(_project_payload(open_project(Path(project_path))))
            )
        if tool_name == "source_add":
            project_path = _string_argument(arguments, "project_path")
            source_path = _string_argument(arguments, "source_path")
            import_mode = ImportMode(_string_argument(arguments, "import_mode"))
            source_expected_revision = _integer_argument(arguments, "expected_revision")
            project = add_source(
                Path(project_path),
                Path(source_path),
                import_mode,
                expected_revision=source_expected_revision,
            )
            return _response(request_id, _tool_result(_project_payload(project)))
    except (OSError, ProjectError, ValueError):
        return _response(
            request_id,
            _tool_result(_versioned_error("project_operation_failed"), is_error=True),
        )
    return _response(
        request_id,
        _tool_result({"ok": False, "error": {"code": "unknown_tool"}}, is_error=True),
    )


def _string_argument(arguments: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{key} is required")
    return value


def _reject_unexpected_arguments(arguments: Mapping[str, Any], *allowed: str) -> None:
    """Keep direct tool calls consistent with a closed MCP input schema."""
    if set(arguments).difference(allowed):
        raise ValueError("unexpected argument")


def _integer_argument(arguments: Mapping[str, Any], key: str) -> int:
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidArgumentType(f"{key} is required")
    return value


def _boolean_argument(arguments: Mapping[str, Any], key: str) -> bool:
    value = arguments.get(key)
    if not isinstance(value, bool):
        raise _InvalidArgumentType(f"{key} is required")
    return value


def _optional_boolean_argument(
    arguments: Mapping[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise _InvalidArgumentType(f"{key} must be a boolean")
    return value


def _string_list_argument(arguments: Mapping[str, Any], key: str) -> list[str]:
    value = arguments.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} is required")
    return value


def _string_array_argument(arguments: Mapping[str, Any], key: str) -> list[str]:
    value = arguments.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return value


def _object_list_argument(arguments: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = arguments.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{key} is required")
    return value


def _object_argument(arguments: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = arguments.get(key)
    if not isinstance(value, dict):
        raise _InvalidArgumentType(f"{key} is required")
    return value


def _optional_string_argument(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_object_argument(arguments: Mapping[str, Any], key: str) -> dict[str, object] | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _InvalidArgumentType(f"{key} must be an object")
    return value


def _optional_object_list_argument(
    arguments: Mapping[str, Any], key: str
) -> list[dict[str, Any]] | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be an array of objects")
    return value


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


def _versioned_error(code: str) -> dict[str, object]:
    payload = health()
    payload["ok"] = False
    payload["error"] = {"code": code}
    return payload


def _error_tool_result(error: WorkflowError | MediaOperationError) -> dict[str, object]:
    payload = _versioned_error(error.code)
    if isinstance(error, WorkflowError):
        content_payload = {
            **payload,
            "error": {"code": error.code, "message": str(error)},
        }
    else:
        content_payload = payload
    return _tool_result(
        payload,
        is_error=True,
        content_payload=content_payload,
    )


def _configure_stdin_utf8() -> None:
    reconfigure = getattr(sys.stdin, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")


def main() -> None:
    _configure_stdin_utf8()
    response_lock = threading.Lock()
    request_ready = threading.Condition()
    no_active_request = object()
    pending_business: Mapping[str, Any] | None = None
    business_active = False
    active_business_request_id: object = no_active_request
    business_response_abandoned = False
    reader_done = False
    reader_failure: BaseException | None = None

    def write_response(response: dict[str, object] | None) -> None:
        if response is None:
            return
        serialized = json.dumps(response, ensure_ascii=False)
        with response_lock:
            sys.stdout.reconfigure(encoding="utf-8", errors="strict")
            sys.stdout.write(f"{serialized}\n")
            sys.stdout.flush()

    def read_requests() -> None:
        nonlocal active_business_request_id
        nonlocal business_active
        nonlocal business_response_abandoned
        nonlocal pending_business
        nonlocal reader_done
        nonlocal reader_failure
        try:
            for line in sys.stdin:
                response: dict[str, object] | None
                try:
                    request = json.loads(line)
                    if not isinstance(request, Mapping):
                        response = _error(None, -32600, "Invalid Request")
                    elif _is_notification(request):
                        if request.get("method") == "notifications/cancelled":
                            params = request.get("params")
                            if isinstance(params, Mapping) and "requestId" in params:
                                cancelled_id = params["requestId"]
                                with request_ready:
                                    if (
                                        business_active
                                        and active_business_request_id
                                        is not no_active_request
                                        and _same_request_id(
                                            cancelled_id,
                                            active_business_request_id,
                                        )
                                    ):
                                        business_response_abandoned = True
                        response = None
                    elif request.get("method") == "tools/call":
                        with request_ready:
                            if business_active:
                                response = _error(
                                    request.get("id"),
                                    TRANSPORT_BUSY_ERROR,
                                    "Server busy",
                                )
                            else:
                                business_active = True
                                active_business_request_id = request.get("id")
                                business_response_abandoned = False
                                pending_business = request
                                request_ready.notify()
                                response = None
                    else:
                        response = handle_request(request)
                except json.JSONDecodeError:
                    response = _error(None, -32700, "Parse error")
                except Exception as error:  # noqa: BLE001 - stdio line boundary must return JSON-RPC error
                    print(f"roughcut MCP error: {error}", file=sys.stderr)
                    response = _error(None, -32603, "Internal error")
                write_response(response)
        except BaseException as error:  # noqa: BLE001 - reader boundary must propagate transport failures
            with request_ready:
                reader_failure = error
                request_ready.notify()
        finally:
            with request_ready:
                reader_done = True
                request_ready.notify()

    def request_is_ready() -> bool:
        return (
            pending_business is not None
            or reader_failure is not None
            or reader_done
        )

    reader = threading.Thread(
        target=read_requests,
        name="roughcut-mcp-stdio-reader",
        daemon=True,
    )
    reader.start()

    while True:
        with request_ready:
            request_ready.wait_for(request_is_ready)
            if reader_failure is not None:
                raise reader_failure
            if pending_business is None:
                return
            request = pending_business
            pending_business = None
        try:
            response = handle_request(request)
        except Exception as error:  # noqa: BLE001 - process boundary must return JSON-RPC error
            print(f"roughcut MCP error: {error}", file=sys.stderr)
            response = _error(request.get("id"), -32603, "Internal error")
        except BaseException:
            raise
        with request_ready:
            if not business_response_abandoned:
                write_response(response)
            business_active = False
            active_business_request_id = no_active_request
            business_response_abandoned = False
            if reader_done:
                request_ready.notify()


if __name__ == "__main__":
    main()
