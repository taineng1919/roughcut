export type DraftEditOperation =
  | "delete"
  | "move"
  | "insert"
  | "section_reorder"
  | "section_rename"
  | "section_split"
  | "section_merge"
  | "section_delete";

export type DraftEditInteractionPhase =
  | "selection_resolve"
  | "pointermove_hit_test"
  | "drop_http_server"
  | "dom_patch";

export interface DraftEditInteractionTiming {
  selection_resolve_ms: number;
  pointermove_hit_test_ms: number[];
  drop_http_server_ms: number;
  dom_patch_ms: number;
}

export class DraftEditPerformanceTrace {
  private timing: DraftEditInteractionTiming = {
    selection_resolve_ms: 0,
    pointermove_hit_test_ms: [],
    drop_http_server_ms: 0,
    dom_patch_ms: 0,
  };

  reset(): void {
    this.timing = {
      selection_resolve_ms: 0,
      pointermove_hit_test_ms: [],
      drop_http_server_ms: 0,
      dom_patch_ms: 0,
    };
  }

  record(phase: DraftEditInteractionPhase, milliseconds: number): void {
    if (!Number.isFinite(milliseconds) || milliseconds < 0) {
      throw new Error("初稿编辑交互耗时数据无效");
    }
    if (phase === "selection_resolve") this.timing.selection_resolve_ms = milliseconds;
    else if (phase === "pointermove_hit_test") this.timing.pointermove_hit_test_ms.push(milliseconds);
    else if (phase === "drop_http_server") this.timing.drop_http_server_ms = milliseconds;
    else this.timing.dom_patch_ms = milliseconds;
  }

  snapshot(): DraftEditInteractionTiming {
    return {
      selection_resolve_ms: this.timing.selection_resolve_ms,
      pointermove_hit_test_ms: [...this.timing.pointermove_hit_test_ms],
      drop_http_server_ms: this.timing.drop_http_server_ms,
      dom_patch_ms: this.timing.dom_patch_ms,
    };
  }
}

export function draftEditPerformanceDataset(
  operation: DraftEditOperation,
  timing: DraftEditInteractionTiming,
): Record<string, string> {
  if (timing.pointermove_hit_test_ms.length === 0) {
    throw new Error("初稿编辑缺少 pointermove 命中耗时");
  }
  return {
    editOperation: operation,
    editSelectionResolveMs: duration(timing.selection_resolve_ms),
    editPointermoveHitTestP95Ms: duration(p95Milliseconds(timing.pointermove_hit_test_ms)),
    editPointermoveHitTestSamples: String(timing.pointermove_hit_test_ms.length),
    editDropHttpServerMs: duration(timing.drop_http_server_ms),
    editDomPatchMs: duration(timing.dom_patch_ms),
  };
}

export function p95Milliseconds(values: readonly number[]): number {
  if (values.length === 0 || values.some((value) => !Number.isFinite(value) || value < 0)) {
    throw new Error("初稿编辑 pointermove 耗时数据无效");
  }
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)]!;
}

export interface DraftEditServerTiming {
  selection_caret_revalidation_ms: number;
  immutable_child_write_fsync_ms: number;
  project_brief_transcript_context_validation_ms: number;
  workflow_snapshot_refresh_ms: number;
  draft_snapshot_rebuild_ms: number;
  server_before_response_ms: number;
}

export function serverDraftEditTimingDataset(
  operation: DraftEditOperation,
  timing: DraftEditServerTiming,
): Record<string, string> {
  return {
    editOperation: operation,
    editSelectionCaretRevalidationMs: duration(timing.selection_caret_revalidation_ms),
    editImmutableChildWriteFsyncMs: duration(timing.immutable_child_write_fsync_ms),
    editProjectBriefTranscriptContextValidationMs: duration(
      timing.project_brief_transcript_context_validation_ms,
    ),
    editWorkflowSnapshotRefreshMs: duration(timing.workflow_snapshot_refresh_ms),
    editDraftSnapshotRebuildMs: duration(timing.draft_snapshot_rebuild_ms),
    editServerBeforeResponseMs: duration(timing.server_before_response_ms),
  };
}

function duration(value: number): string {
  if (!Number.isFinite(value) || value < 0) {
    throw new Error("初稿编辑耗时数据无效");
  }
  return String(Math.round(value));
}
