import { describe, expect, it } from "vitest";

import {
  draftEditPerformanceDataset,
  DraftEditPerformanceTrace,
  serverDraftEditTimingDataset,
} from "./draft-edit-performance";

describe("draft edit performance trace", () => {
  it("keeps every server phase attributable to the exact edit operation", () => {
    expect(serverDraftEditTimingDataset("move", {
      selection_caret_revalidation_ms: 1.4,
      immutable_child_write_fsync_ms: 8.6,
      project_brief_transcript_context_validation_ms: 2.2,
      workflow_snapshot_refresh_ms: 0.4,
      draft_snapshot_rebuild_ms: 4.7,
      server_before_response_ms: 18.1,
    })).toEqual({
      editOperation: "move",
      editSelectionCaretRevalidationMs: "1",
      editImmutableChildWriteFsyncMs: "9",
      editProjectBriefTranscriptContextValidationMs: "2",
      editWorkflowSnapshotRefreshMs: "0",
      editDraftSnapshotRebuildMs: "5",
      editServerBeforeResponseMs: "18",
    });
  });

  it("rejects missing or invalid phase timings instead of hiding them", () => {
    expect(() => serverDraftEditTimingDataset("delete", {
      selection_caret_revalidation_ms: Number.NaN,
      immutable_child_write_fsync_ms: 1,
      project_brief_transcript_context_validation_ms: 1,
      workflow_snapshot_refresh_ms: 1,
      draft_snapshot_rebuild_ms: 1,
      server_before_response_ms: 1,
    })).toThrow(/耗时/);
  });

  it("records four bounded client-side phases for a long Chinese section fixture", () => {
    const fixture = {
      paragraphs: Array.from({ length: 10 }, (_, index) =>
        `第${index + 1}段：这是一个包含中文标点、空格、数字 2026 和 emoji 😀 的长正文，用于真实拖动性能回归；`
          .repeat(3),
      ),
      sections: ["第一章", "第二章", "第三章", "空章节"],
    };
    expect(fixture.paragraphs.every((paragraph) => paragraph.length >= 80)).toBe(true);
    expect(fixture.sections).toHaveLength(4);

    const trace = new DraftEditPerformanceTrace();
    trace.record("selection_resolve", 7.4);
    for (let index = 0; index < fixture.paragraphs.length; index += 1) {
      trace.record("pointermove_hit_test", 5 + (index % 3));
    }
    trace.record("drop_http_server", 31.6);
    trace.record("dom_patch", 4.8);

    expect(draftEditPerformanceDataset("move", trace.snapshot())).toEqual({
      editOperation: "move",
      editSelectionResolveMs: "7",
      editPointermoveHitTestP95Ms: "7",
      editPointermoveHitTestSamples: "10",
      editDropHttpServerMs: "32",
      editDomPatchMs: "5",
    });
  });
});
