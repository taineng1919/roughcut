import { describe, expect, it } from "vitest";

import {
  coverageSummary,
  proposalBoundarySummary,
  proposalConfirmationReady,
  proposalDiffSummary,
} from "./proposal-boundary";
import type { ReviewPayload } from "./types";
import type { ReadableParagraph } from "./workflow-types";

const review: ReviewPayload = {
  schema_version: 2,
  project: { project_id: "project_fixture", name: "Fixture", revision: 9 },
  basis: { type: "proposal", id: "proposal_current" },
  proposal: {
    schema_version: 2,
    proposal_id: "proposal_current",
    clips: [{
      clip_id: "clip_a",
      source_id: "src_a",
      transcript_version_id: "tr_a",
      segment_id: "seg_a",
      source_in_ticks: 0,
      source_out_ticks: 120_000,
      reason: "Content Draft source excerpt",
      display_text: "canonical A",
    }],
    total_duration_ticks: 120_000,
  },
  context_hash: "a".repeat(64),
  allowed_operations: ["select", "trim", "reorder"],
  transcript: [],
  sources: [{
    source_id: "src_a",
    transcript_version_id: "tr_a",
    display_name: "素材 A",
    kind: "video",
    duration_ticks: 600_000,
    tags: [],
    note: "",
    media_url: "/media/src_a",
    playback_kind: "original",
    proxy_profile: null,
  }],
  timeline: {
    total_duration_ticks: 120_000,
    spans: [{
      clip_id: "clip_a",
      source_id: "src_a",
      source_in_ticks: 0,
      source_out_ticks: 120_000,
      output_in_ticks: 0,
      output_out_ticks: 120_000,
    }],
  },
  requires_new_proposal: false,
};

function paragraph(status: ReadableParagraph["adoption_status"]): ReadableParagraph {
  return {
    paragraph_id: `paragraph_${status}`,
    display_number: "P001",
    source_id: "src_a",
    transcript_version_id: "tr_a",
    source_display_name: "素材 A",
    local_speaker_id: "spk_0",
    local_speaker_ids: ["spk_0"],
    person_id: "person_a",
    person_name: "人物 A",
    text: status,
    start_ticks: 0,
    end_ticks: 120_000,
    refs: [{
      source_id: "src_a",
      transcript_version_id: "tr_a",
      segment_id: `seg_${status}`,
      start_ticks: 0,
      end_ticks: 120_000,
    }],
    adoption_status: status,
  };
}

describe("Proposal boundary presentation", () => {
  it("shows the exact Proposal identity, complete counts, bindings and draft origin", () => {
    expect(proposalBoundarySummary(review, "draft_confirmed")).toEqual({
      basisType: "proposal",
      artifactId: "proposal_current",
      proposalId: "proposal_current",
      contentDraftId: "draft_confirmed",
      projectRevision: 9,
      schemaVersion: 2,
      clipCount: 1,
      totalDurationTicks: 120_000,
      bindings: [{ source_id: "src_a", transcript_version_id: "tr_a" }],
      renderAuthorized: false,
    });
  });

  it("uses backend diff facts verbatim and labels the initial no-base case", () => {
    expect(proposalDiffSummary({
      applicable: false,
      reason: "no_active_decision",
      project_revision: 9,
    })).toEqual({ kind: "none", label: "首次方案，无上一版可比较" });

    const summary = proposalDiffSummary({
      applicable: true,
      project_revision: 9,
      proposal_diff: {
        base_edit_version_id: "edit_base",
        proposal_id: "proposal_current",
        schema_version: 2,
        before_clip_count: 99,
        after_clip_count: 1,
        before_duration_ticks: 999_999,
        after_duration_ticks: 120_000,
        duration_delta_ticks: -879_999,
        added: [{ clip_id: "server_added" }],
        removed: [{ clip_id: "server_removed" }],
        changed: [{ clip_id: "server_changed", fields: ["reason"] }],
        order_changed: true,
        before_order: ["server_removed"],
        after_order: ["server_added"],
      },
    });
    expect(summary).toMatchObject({
      kind: "diff",
      baseEditVersionId: "edit_base",
      addedCount: 1,
      removedCount: 1,
      changedCount: 1,
      orderChanged: true,
      durationDeltaTicks: -879_999,
    });
  });

  it("summarizes only core-returned coverage statuses and requires complete pagination", () => {
    expect(coverageSummary([
      paragraph("adopted"),
      paragraph("partial"),
      paragraph("unadopted"),
      paragraph("unadopted"),
    ], true)).toEqual({ adopted: 1, partial: 1, unadopted: 2, total: 4 });
    expect(() => coverageSummary([paragraph("adopted")], false)).toThrow(/完整加载/);
  });

  it("keeps Proposal confirmation locked until all boundary evidence is loaded", () => {
    expect(proposalConfirmationReady("proposal", false)).toBe(false);
    expect(proposalConfirmationReady("proposal", true)).toBe(true);
    expect(proposalConfirmationReady("decision", true)).toBe(false);
  });
});
