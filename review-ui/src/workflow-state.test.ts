import { describe, expect, it } from "vitest";

import {
  ReadableTranscriptAccumulator,
  WorkflowPanelState,
  initialDraftEditorSnapshot,
  reviewMode,
  workflowBindingLabels,
  workflowSessionMessage,
  workflowWritesLocked,
} from "./workflow-state";
import type {
  DraftEditorSnapshot,
  ReadableTranscriptPage,
  WorkflowApi,
  WorkflowReviewPayload,
} from "./workflow-types";

function page(offset: number, next: number | null): ReadableTranscriptPage {
  return {
    view_schema_version: 1,
    algorithm_version: 1,
    view_hash: "a".repeat(64),
    project_id: "project_fixture",
    project_revision: 4,
    source_bindings: [{ source_id: "src_a", transcript_version_id: "tr_a" }],
    offset,
    limit: 1,
    total: 2,
    next_cursor: next,
    paragraphs: [{
      paragraph_id: `paragraph_${offset}`,
      display_number: `P00${offset + 1}`,
      source_id: "src_a",
      transcript_version_id: "tr_a",
      source_display_name: "素材 A",
      local_speaker_id: "spk_0",
      local_speaker_ids: ["spk_0"],
      person_id: null,
      person_name: null,
      text: `第${offset + 1}段`,
      start_ticks: offset * 120_000,
      end_ticks: (offset + 1) * 120_000,
      refs: [{
        source_id: "src_a",
        transcript_version_id: "tr_a",
        segment_id: `seg_${offset}`,
        start_ticks: offset * 120_000,
        end_ticks: (offset + 1) * 120_000,
      }],
      adoption_status: "not_applicable",
    }],
  };
}

describe("workflow UI state", () => {
  it("loads the cached draft editor before the slower generic review payload", async () => {
    const calls: string[] = [];
    const snapshot = { review_mode: "draft_editor" } as DraftEditorSnapshot;
    const draftApi: WorkflowApi = async <T>(path: string) => {
      calls.push(path);
      return snapshot as T;
    };
    const loaded = await initialDraftEditorSnapshot(draftApi);
    expect(loaded).toBe(snapshot);
    expect(calls).toEqual(["/api/workflow/draft-editor"]);

    const unavailableApi: WorkflowApi = async () => {
      throw new Error("not a workflow draft editor");
    };
    const unavailable = await initialDraftEditorSnapshot(unavailableApi);
    expect(unavailable).toBeNull();
  });

  it("dispatches workflow snapshots additively and leaves old Review snapshots unchanged", () => {
    expect(reviewMode({ review_mode: "workflow", workflow_schema_version: 1 })).toBe("workflow");
    expect(reviewMode({ schema_version: 1, basis: { type: "proposal" } })).toBe("artifact");
  });

  it("keeps paragraph identity and one view hash across pages", () => {
    const pages = new ReadableTranscriptAccumulator();
    pages.append(page(0, 1));
    pages.append(page(1, null));
    expect(pages.paragraphs.map((paragraph) => paragraph.paragraph_id)).toEqual([
      "paragraph_0",
      "paragraph_1",
    ]);
    expect(pages.viewHash).toBe("a".repeat(64));
    expect(pages.complete).toBe(true);
    expect(() => pages.append({ ...page(1, null), view_hash: "b".repeat(64) }))
      .toThrow("原始转录稿在分页读取期间发生了变化");

    const filtered = new ReadableTranscriptAccumulator();
    filtered.append({ ...page(0, null), total: 1, next_cursor: null });
    expect(filtered.paragraphs[0]?.paragraph_id).toBe(pages.paragraphs[0]?.paragraph_id);
  });

  it("locks writes on stale and keeps an explicit narrow-screen tab", () => {
    const snapshot = {
      session: {
        status: "stale",
        read_only: true,
        snapshot_revision: 4,
        current_revision: 5,
        reasons: ["speaker_map", "context_hash"],
      },
    } as WorkflowReviewPayload;
    expect(workflowWritesLocked(snapshot, false)).toBe(true);
    expect(workflowSessionMessage(snapshot)).toBe(
      "项目内容已在别处更新，本页已转为只读，请重新打开当前步骤。",
    );
    expect(workflowSessionMessage(snapshot)).not.toContain("revision");
    const panels = new WorkflowPanelState();
    expect(panels.active).toBe("draft");
    panels.select("transcript");
    expect(panels.active).toBe("transcript");
    panels.select("player");
    expect(panels.active).toBe("player");
  });

  it("presents only safe source names and binding IDs", () => {
    const snapshot = {
      source_bindings: [{ source_id: "src_a", transcript_version_id: "tr_a" }],
      sources: [{
        source_id: "src_a",
        display_name: "素材 A",
        locator: "/Users/private/source.mp4",
        token: "secret-token",
      }],
    } as unknown as WorkflowReviewPayload;
    const labels = workflowBindingLabels(snapshot).join("\n");
    expect(labels).toContain("素材 A · src_a / tr_a");
    expect(labels).not.toContain("/Users/private");
    expect(labels).not.toContain("secret-token");
  });
});
