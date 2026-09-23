import { describe, expect, it } from "vitest";

import {
  DecisionReviewState,
  ReviewDraft,
  deleteOperation,
  moveClip,
  parseIntegerTicks,
  removeClip,
  reorderOperation,
  restoreOperation,
  trimOperation,
} from "./sequence";
import type { Clip, EditHistory, ReviewEditResponse, ReviewPayload } from "./types";

const clips: Clip[] = Array.from({ length: 10 }, (_, index) => ({
  clip_id: `clip_${index + 1}`,
  source_id: "src_a",
  transcript_version_id: "tr_a",
  segment_id: `seg_${index + 1}`,
  source_in_ticks: index * 120_000,
  source_out_ticks: (index + 1) * 120_000,
  reason: "fixture",
  display_text: `第${index + 1}句`,
}));

describe("complete proposal draft", () => {
  it("deletes and reorders a copied complete snapshot", () => {
    const deleted = removeClip(clips, "clip_5");
    const moved = moveClip(deleted, 8, 0);
    expect(moved).toHaveLength(9);
    expect(moved[0]?.clip_id).toBe("clip_10");
    expect(clips.map((clip) => clip.clip_id)).toEqual(
      Array.from({ length: 10 }, (_, index) => `clip_${index + 1}`),
    );
  });

  it("keeps proposal and confirmation as separate revision-safe actions", () => {
    const draft = new ReviewDraft(clips, 7, true, "proposal_original");
    draft.delete("clip_5");
    draft.move(8, 0);
    expect(draft.dirty).toBe(true);
    expect(() => draft.confirmationPayload()).toThrow(/保存并完整核对待确认剪辑方案/);

    draft.proposalAccepted("proposal_revised", draft.clips, 7);
    expect(draft.dirty).toBe(false);
    expect(draft.projectRevision).toBe(7);
    expect(draft.confirmationPayload()).toEqual({ proposal_id: "proposal_revised" });

    draft.confirmedAt(8);
    expect(draft.confirmed).toBe(true);
    expect(draft.projectRevision).toBe(8);
  });

  it("blocks reorder when the brief did not authorize it", () => {
    const draft = new ReviewDraft(clips, 7, false, "proposal_original");
    expect(() => draft.move(1, 0)).toThrow(/剪辑要求不允许调整同期声顺序/);
  });
});

function decisionReview(revision = 7, activeId = "edit_current"): ReviewPayload {
  return {
    schema_version: 1,
    project: { project_id: "project_fixture", name: "Fixture", revision },
    basis: { type: "decision", id: activeId },
    proposal: {
      schema_version: 1,
      proposal_id: "proposal_fixture",
      clips,
      total_duration_ticks: 1_200_000,
    },
    context_hash: "0".repeat(64),
    allowed_operations: ["delete", "reorder", "trim"],
    transcript: [],
    sources: [],
    timeline: {
      total_duration_ticks: 1_200_000,
      spans: clips.map((clip, index) => ({
        clip_id: clip.clip_id,
        source_id: clip.source_id,
        source_in_ticks: clip.source_in_ticks,
        source_out_ticks: clip.source_out_ticks,
        output_in_ticks: index * 120_000,
        output_out_ticks: (index + 1) * 120_000,
      })),
    },
    requires_new_proposal: true,
  };
}

function editHistory(revision = 7, activeId = "edit_current"): EditHistory {
  return {
    project_revision: revision,
    active_edit_version_id: activeId,
    active_schema_version: 1,
    can_undo: true,
    can_redo: false,
    ancestors: [],
    redo_stack: [],
    restorable_clips: [{
      clip_id: "clip_removed",
      source_id: "src_a",
      transcript_version_id: "tr_a",
      segment_id: "seg_removed",
      source_in_ticks: 1_200_000,
      source_out_ticks: 1_320_000,
      from_edit_version_id: "edit_parent",
    }],
    current_clips: clips,
    total_duration_ticks: 1_200_000,
  };
}

describe("decision direct editing", () => {
  it("builds exact delete, restore, reorder, and integer trim operations", () => {
    expect(deleteOperation("clip_3")).toEqual({ type: "delete", clip_id: "clip_3" });
    expect(restoreOperation("clip_removed", "clip_4")).toEqual({
      type: "restore",
      clip_id: "clip_removed",
      insert_before_clip_id: "clip_4",
    });
    expect(restoreOperation("clip_removed", null)).toEqual({
      type: "restore",
      clip_id: "clip_removed",
      insert_before_clip_id: null,
    });
    expect(reorderOperation(clips, 2, 0, true)).toEqual({
      type: "reorder",
      ordered_clip_ids: [
        "clip_3",
        "clip_1",
        "clip_2",
        ...Array.from({ length: 7 }, (_, index) => `clip_${index + 4}`),
      ],
    });
    expect(() => reorderOperation(clips, 2, 0, false)).toThrow(/does not allow reorder/);
    expect(trimOperation("clip_2", "120001", "239999")).toEqual({
      type: "trim",
      clip_id: "clip_2",
      source_in_ticks: 120_001,
      source_out_ticks: 239_999,
    });
    expect(parseIntegerTicks("120000")).toBe(120_000);
    expect(() => parseIntegerTicks("1.5")).toThrow(/integer ticks/);
    expect(() => parseIntegerTicks("true")).toThrow(/integer ticks/);
  });

  it("binds every write to the displayed revision and active Decision", () => {
    const state = new DecisionReviewState(decisionReview(), editHistory());
    expect(state.changePayload(deleteOperation("clip_4"))).toEqual({
      active_edit_version_id: "edit_current",
      expected_revision: 7,
      operation: { type: "delete", clip_id: "clip_4" },
    });
    expect(state.navigationPayload()).toEqual({
      active_edit_version_id: "edit_current",
      expected_revision: 7,
    });
  });

  it("replaces the complete server state after success and stays unchanged on errors", () => {
    const state = new DecisionReviewState(decisionReview(), editHistory());
    const originalReview = state.review;
    const originalHistory = state.history;
    const error = { code: "stale_review", message: "stale" };
    expect(error.code).toBe("stale_review");
    expect(state.review).toBe(originalReview);
    expect(state.history).toBe(originalHistory);

    const nextReview = decisionReview(8, "edit_next");
    const nextHistory = editHistory(8, "edit_next");
    const response: ReviewEditResponse = {
      review: nextReview,
      edit_history: nextHistory,
    };
    state.replace(response);
    expect(state.review).toEqual(nextReview);
    expect(state.history).toEqual(nextHistory);
    expect(state.review).not.toBe(nextReview);
    expect(state.history).not.toBe(nextHistory);
  });

  it("rejects partial or inconsistent replacements", () => {
    const state = new DecisionReviewState(decisionReview(), editHistory());
    expect(() => state.replace({
      review: decisionReview(8, "edit_next"),
      edit_history: editHistory(8, "edit_other"),
    })).toThrow(/已确认剪辑在读取期间发生了变化/);
    expect(state.review.basis.id).toBe("edit_current");
    expect(state.history.project_revision).toBe(7);
  });
});
