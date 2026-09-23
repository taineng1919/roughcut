import type { ReviewSource } from "./types";

export interface SourceBinding {
  source_id: string;
  transcript_version_id: string;
}

export interface EditBrief {
  schema_version: 1;
  brief_id: string;
  theme: string;
  target_duration_ticks: number;
  focus: string[];
  allow_reorder: boolean;
}

export interface ContentDraftRef {
  source_id: string;
  transcript_version_id: string;
  segment_id: string;
  start_ticks: number;
  end_ticks: number;
}

export interface SourceExcerptBlock {
  block_id: string;
  kind: "source_excerpt";
  refs: ContentDraftRef[];
  canonical_text: string;
  display_text?: string;
}

export interface SectionTitleBlock {
  block_id: string;
  kind: "section_title";
  title: string;
}

export interface NarrationBlock {
  block_id: string;
  kind: "narration";
  text: string;
  status: "draft" | "approved" | "recorded";
  recorded_refs: ContentDraftRef[];
}

export type ContentDraftBlock = SourceExcerptBlock | NarrationBlock | SectionTitleBlock;

export interface ContentDraft {
  schema_version: 1 | 2;
  content_draft_id: string;
  parent_draft_id: string | null;
  base_project_revision: number;
  confirmed_by_user: boolean;
  brief_snapshot: EditBrief;
  source_bindings: SourceBinding[];
  context_hash: string;
  blocks: ContentDraftBlock[];
}

export interface ContentDraftState {
  content_draft: ContentDraft;
  project_revision: number;
  status: "current" | "stale";
  stale_reasons: string[];
}

export interface WorkflowSession {
  status: "current" | "stale";
  read_only: boolean;
  snapshot_revision: number;
  current_revision: number;
  reasons: string[];
}

export type WorkflowStageStatus =
  | "尚未开始"
  | "待您确认"
  | "已完成，可直接使用"
  | "内容已变更，需要重新确认"
  | "正在处理";

export interface WorkflowStageSummary {
  key: "sources" | "prepare" | "transcript" | "brief" | "draft" | "proposal" | "decision" | "render";
  title: string;
  status: WorkflowStageStatus;
  technical_details: Record<string, unknown>;
}

export interface WorkflowPreparationSource {
  display_name: string;
  tags: string[];
  note: string;
  playback_status: string;
  transcript_status: string;
  mapped_person_count: number;
  mapped_people: string[];
  unmapped_local_speaker_count: number;
  technical_details: Record<string, unknown>;
}

export interface WorkflowReviewPayload {
  workflow_schema_version: 1;
  review_mode: "workflow";
  project: { project_id: string; name: string; revision: number };
  source_bindings: SourceBinding[];
  sources: ReviewSource[];
  brief: EditBrief | null;
  content_draft: ContentDraftState | null;
  context_hash: string | null;
  readable_transcript: {
    endpoint: string;
    selection_endpoint: string;
    pagination: { offset_unit: "paragraph"; max_limit: number };
    filters: string[];
    overlay_bases: string[];
    selection_offset_unit: "unicode_code_point";
  };
  allowed_operations: string[];
  workflow_stages: WorkflowStageSummary[];
  preparation: { sources: WorkflowPreparationSource[] };
  session: WorkflowSession;
}

export interface ReadableParagraph {
  paragraph_id: string;
  display_number: string;
  source_id: string;
  transcript_version_id: string;
  source_display_name: string;
  local_speaker_id: string | null;
  local_speaker_ids: string[];
  person_id: string | null;
  person_name: string | null;
  text: string;
  start_ticks: number;
  end_ticks: number;
  refs: ContentDraftRef[];
  adoption_status: "adopted" | "partial" | "unadopted" | "not_applicable";
}

export interface ReadableTranscriptPage {
  view_schema_version: 1;
  algorithm_version: number;
  view_hash: string;
  project_id: string;
  project_revision: number;
  source_bindings: SourceBinding[];
  offset: number;
  limit: number;
  total: number;
  next_cursor: number | null;
  paragraphs: ReadableParagraph[];
}

export interface ResolvedSelectionRef extends ContentDraftRef {
  canonical_text: string;
}

export interface SelectionResolution {
  view_hash: string;
  mode: "exact" | "expanded_to_segments";
  canonical_text: string;
  refs: ResolvedSelectionRef[];
  warnings: string[];
}

export interface WorkflowFilters {
  source_ids?: string[];
  person_ids?: string[];
  adoption_statuses?: string[];
  keyword?: string;
}

export interface BriefMutationResponse {
  workflow: WorkflowReviewPayload;
}

export interface DraftMutationResponse {
  content_draft_mutation: ContentDraftState & { changed: boolean };
  workflow: WorkflowReviewPayload;
}

export type WorkflowApi = <T = unknown>(
  path: string,
  options?: RequestInit,
) => Promise<T>;

export interface DraftEditorSourceRun {
  block_id?: string;
  source_id: string;
  source_display_name: string;
  paragraph_id: string;
  start_ticks: number;
  end_ticks: number;
  start_offset: number;
  end_offset: number;
  source_start_offset: number;
  source_end_offset: number;
  text: string;
  refs: ContentDraftRef[];
}

export interface DraftEditorParagraph {
  paragraph_id: string;
  kind: "source_excerpt" | "narration" | "section_title";
  person: {
    person_id: string | null;
    name: string | null;
    role: string | null;
    local_speaker_id: string | null;
  };
  text: string;
  section_title: string | null;
  narration_status: "draft" | "approved" | "recorded" | null;
  block_id?: string;
  source_runs: DraftEditorSourceRun[];
  exact_refs: ContentDraftRef[];
}

export interface DraftEditorSnapshot {
  editor_schema_version: 1;
  review_mode: "draft_editor";
  project: { name: string };
  brief: {
    theme: string;
    target_duration_ticks: number;
    focus: string[];
    allow_reorder: boolean;
  };
  candidate: {
    candidate_id: string;
    parent_candidate_id: string | null;
    display_title: string | null;
    confirmed_by_user: boolean;
    has_unrecorded_narration: boolean;
  };
  paragraphs: DraftEditorParagraph[];
  blocks?: ContentDraftBlock[];
  sections?: { heading_block_id: string; title: string }[];
  sources: ReviewSource[];
  history: {
    can_undo: boolean;
    can_redo: boolean;
    redo_scope: "draft_workspace";
  };
  workspace: DraftWorkspaceMutationBasis;
  transcript_browser: {
    read_endpoint: string;
    window_endpoint: string;
    search_endpoint: string;
    selection_endpoint: string;
    pagination: { offset_unit: "paragraph"; max_limit: number };
  };
  candidate_handoff: {
    endpoint: "/api/workflow/draft-candidate-select";
    requires_exact_parent: true;
  };
}

export interface ArtifactRef {
  artifact_id: string;
  schema_version: number;
  content_hash: string;
}

export interface DraftWorkspaceMutationBasis {
  expected_checkpoint_ref: {
    generation: number;
    checkpoint_hash: string;
  };
  expected_current_candidate_ref: ArtifactRef;
}

export interface DraftEditorRequestPoint {
  paragraph_id: string;
  offset: number;
  offset_encoding: "utf16";
}

export interface DraftEditorSelectionRequest {
  anchor: DraftEditorRequestPoint;
  focus: DraftEditorRequestPoint;
}

export interface DraftEditorDisplayPoint {
  paragraph_id: string;
  character_offset: number;
  utf16_offset: number;
}

export interface DraftEditorSourceCaret {
  paragraph_id: string;
  boundary_id: string;
  source_id: string | null;
  transcript_version_id: string | null;
  character_offset: number;
  utf16_offset: number;
  degraded: boolean;
  degradation_reason: string | null;
}

export interface DraftEditorSelectionResponse {
  candidate_id: string;
  surface: "draft" | "source";
  resolution: {
    direction: "forward" | "backward";
    canonical_text: string;
    refs: ResolvedSelectionRef[];
    start_caret: DraftEditorSourceCaret | null;
    end_caret: DraftEditorSourceCaret | null;
    adjusted: boolean;
    degraded: boolean;
    degradation_reasons: string[];
  };
  display_range: {
    anchor: DraftEditorDisplayPoint;
    focus: DraftEditorDisplayPoint;
  };
  resolved_display_range?: {
    anchor: DraftEditorDisplayPoint;
    focus: DraftEditorDisplayPoint;
  };
  correspondence_groups: DraftEditorCorrespondenceGroup[];
  resolution_hash?: string;
  narration_block_id?: string | null;
  narration_text?: string | null;
  narration_status?: "draft" | "approved" | "recorded" | null;
}

export interface DraftEditorCorrespondenceGroup {
  source_id: string;
  source_display_name: string;
  paragraph_id: string;
  start_offset: number;
  end_offset: number;
}

export interface DraftEditorCaret {
  candidate_id: string;
  paragraph_id: string;
  character_offset: number;
  utf16_offset: number;
  boundary_id: string;
  degraded: boolean;
  degradation_reason: string | null;
}

export interface DraftEditorSearchMatch {
  match_id: string;
  paragraph_id: string;
  occurrence: number;
  start_offset: number;
  end_offset: number;
  context: string;
  source_id: string | null;
  source_display_name: string | null;
  person_name: string | null;
  start_ticks: number | null;
}

export interface DraftEditorSearchPage {
  surface: "draft" | "source";
  query: string;
  offset: number;
  limit: number;
  total: number;
  next_cursor: number | null;
  matches: DraftEditorSearchMatch[];
}

export interface DraftTranscriptWindow {
  candidate_id: string;
  source_id: string;
  offset: number;
  limit: number;
  total: number;
  next_cursor: number | null;
  previous_cursor: number | null;
  located_paragraph_id: string | null;
  paragraphs: ReadableParagraph[];
}
