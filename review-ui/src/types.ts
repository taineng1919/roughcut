export interface Clip {
  clip_id: string;
  source_id: string;
  transcript_version_id: string;
  segment_id: string;
  source_in_ticks: number;
  source_out_ticks: number;
  reason: string;
  display_text: string;
}

export interface TimelineSpan {
  clip_id: string;
  source_id: string;
  source_in_ticks: number;
  source_out_ticks: number;
  output_in_ticks: number;
  output_out_ticks: number;
}

export interface TranscriptSegment {
  source_id: string;
  transcript_version_id: string;
  segment_id: string;
  text: string;
  original_text: string;
  corrected_text: string | null;
  start_ticks: number;
  end_ticks: number;
  speaker: string | null;
  local_speaker_id: string | null;
  person_id: string | null;
  person_name: string | null;
}

export interface ActiveTranscriptSegment {
  segment_id: string;
  start_ticks: number;
  end_ticks: number;
  original_text: string;
  corrected_text: string | null;
}

export interface TranscriptVersionSummary {
  transcript_version_id: string;
  parent_version_id: string | null;
  segment_count: number;
  active: boolean;
  kind: "original_asr" | "user_correction";
}

export interface TranscriptVersionSource {
  source_id: string;
  display_name: string;
  frozen_transcript_version_id: string;
  active_transcript_version_id: string | null;
  versions: TranscriptVersionSummary[];
  active_segments: ActiveTranscriptSegment[];
}

export interface TranscriptMismatch {
  source_id: string;
  referenced_transcript_version_id: string;
  active_transcript_version_id: string | null;
}

export interface EditReferenceStatus {
  status: "none" | "current" | "stale";
  mismatches: TranscriptMismatch[];
}

export interface ReviewSessionState {
  status: "current" | "stale";
  read_only: boolean;
  snapshot_revision: number;
  current_revision: number;
  mismatches: TranscriptMismatch[];
}

export interface TranscriptReviewPayload {
  project_revision: number;
  sources: TranscriptVersionSource[];
  edit_reference_status: EditReferenceStatus;
  review_session: ReviewSessionState;
  transcript_mutation?: {
    changed: boolean;
    project_revision: number;
    transcript: {
      transcript_version_id: string;
      source_id: string;
      parent_version_id: string | null;
      segment_count: number;
    };
  };
  transcript_activation?: {
    source_id: string;
    transcript_version_id: string;
    project_revision: number;
    changed: boolean;
  };
}

export interface ReviewSource {
  source_id: string;
  transcript_version_id: string;
  display_name: string;
  kind: "audio" | "video";
  duration_ticks: number;
  tags: string[];
  note: string;
  media_url: string;
  playback_kind: "original" | "proxy";
  proxy_profile: {
    canvas: { width: number; height: number };
    frame_rate: { numerator: number; denominator: number };
    has_audio: boolean;
  } | null;
}

export interface ReviewPayload {
  schema_version: 1 | 2;
  project: { project_id: string; name: string; revision: number };
  basis: { type: "proposal" | "decision"; id: string };
  proposal: {
    schema_version: 1 | 2;
    proposal_id: string;
    base_project_revision?: number;
    clips: Clip[];
    total_duration_ticks: number;
  };
  context_hash: string;
  allowed_operations: string[];
  transcript: TranscriptSegment[];
  source?: ReviewSource;
  sources: ReviewSource[];
  timeline: { total_duration_ticks: number; spans: TimelineSpan[] };
  requires_new_proposal: boolean;
}

export type EditOperation =
  | { type: "delete"; clip_id: string }
  | { type: "restore"; clip_id: string; insert_before_clip_id: string | null }
  | { type: "reorder"; ordered_clip_ids: string[] }
  | {
    type: "trim";
    clip_id: string;
    source_in_ticks: number;
    source_out_ticks: number;
  };

export interface EditHistoryEntry {
  edit_version_id: string;
  parent_edit_version_id: string | null;
  schema_version: 1 | 2;
  decision_project_revision: number;
  clip_count: number;
  total_duration_ticks: number;
  created_at: string;
}

export interface RestorableClip {
  clip_id: string;
  source_id: string;
  transcript_version_id: string;
  segment_id: string;
  source_in_ticks: number;
  source_out_ticks: number;
  from_edit_version_id: string;
}

export interface EditHistory {
  project_revision: number;
  active_edit_version_id: string | null;
  active_schema_version: 1 | 2 | null;
  can_undo: boolean;
  can_redo: boolean;
  ancestors: EditHistoryEntry[];
  redo_stack: string[];
  restorable_clips: RestorableClip[];
  current_clips: Clip[];
  total_duration_ticks: number;
}

export interface ReviewEditResponse {
  review: ReviewPayload;
  edit_history: EditHistory;
  edit_change?: {
    changed: boolean;
    operation_type: EditOperation["type"];
    project_revision: number;
  };
  edit_navigation?: {
    project_revision: number;
    active_edit_version_id: string;
    edit_redo_stack: string[];
  };
}

export interface RoughcutCandidateHistory {
  can_undo: boolean;
  can_redo: boolean;
  restorable_clips: Clip[];
}

export interface RoughcutStateResponse {
  review: ReviewPayload;
  candidate_history: RoughcutCandidateHistory;
  candidate_change?: {
    changed: boolean;
    operation_type: EditOperation["type"];
    project_revision: number;
    proposal: ReviewPayload["proposal"];
  };
  adoption?: {
    changed: boolean;
    project_revision: number;
  };
}
