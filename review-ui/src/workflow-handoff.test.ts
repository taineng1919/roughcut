import { describe, expect, it } from "vitest";

import {
  parseConfirmedContentDraft,
  parseWorkflowProposalHandoff,
  performWorkflowProposalHandoff,
  workflowProposalEligibility,
} from "./workflow-handoff";
import type { ReviewPayload } from "./types";
import type { WorkflowApi, WorkflowReviewPayload } from "./workflow-types";

function workflow(overrides: Partial<WorkflowReviewPayload> = {}): WorkflowReviewPayload {
  return {
    workflow_schema_version: 1,
    review_mode: "workflow",
    project: { project_id: "project_fixture", name: "Fixture", revision: 4 },
    source_bindings: [
      { source_id: "src_a", transcript_version_id: "tr_a" },
      { source_id: "src_b", transcript_version_id: "tr_b" },
    ],
    sources: [],
    brief: {
      schema_version: 1,
      brief_id: "brief_fixture",
      theme: "主题",
      target_duration_ticks: 240_000,
      focus: ["重点：互动"],
      allow_reorder: true,
    },
    content_draft: {
      content_draft: {
        schema_version: 1,
        content_draft_id: "draft_confirmed",
        parent_draft_id: "draft_candidate",
        base_project_revision: 4,
        confirmed_by_user: true,
        brief_snapshot: {
          schema_version: 1,
          brief_id: "brief_fixture",
          theme: "主题",
          target_duration_ticks: 240_000,
          focus: ["重点：互动"],
          allow_reorder: true,
        },
        source_bindings: [
          { source_id: "src_a", transcript_version_id: "tr_a" },
          { source_id: "src_b", transcript_version_id: "tr_b" },
        ],
        context_hash: "a".repeat(64),
        blocks: [{
          block_id: "block_recorded",
          kind: "narration",
          text: "已录旁白",
          status: "recorded",
          recorded_refs: [{
            source_id: "src_b",
            transcript_version_id: "tr_b",
            segment_id: "seg_b",
            start_ticks: 0,
            end_ticks: 120_000,
          }],
        }],
      },
      project_revision: 4,
      status: "current",
      stale_reasons: [],
    },
    context_hash: "a".repeat(64),
    readable_transcript: {
      endpoint: "/api/workflow/readable-transcript",
      selection_endpoint: "/api/workflow/selection-resolve",
      pagination: { offset_unit: "paragraph", max_limit: 200 },
      filters: ["source_ids", "person_ids", "adoption_statuses", "keyword"],
      overlay_bases: ["proposal", "decision"],
      selection_offset_unit: "unicode_code_point",
    },
    allowed_operations: ["content_draft_read", "content_draft_propose"],
    workflow_stages: [],
    preparation: { sources: [] },
    session: {
      status: "current",
      read_only: false,
      snapshot_revision: 4,
      current_revision: 4,
      reasons: [],
    },
    ...overrides,
  };
}

function review(): ReviewPayload {
  const clips = [{
    clip_id: "clip_a",
    source_id: "src_a",
    transcript_version_id: "tr_a",
    segment_id: "seg_a",
    source_in_ticks: 0,
    source_out_ticks: 120_000,
    reason: "Content Draft source excerpt",
    display_text: "核心返回的原话",
  }];
  return {
    schema_version: 2,
    project: { project_id: "project_fixture", name: "Fixture", revision: 4 },
    basis: { type: "proposal", id: "proposal_exact" },
    proposal: {
      schema_version: 2,
      proposal_id: "proposal_exact",
      clips,
      total_duration_ticks: 120_000,
    },
    context_hash: "a".repeat(64),
    allowed_operations: ["select", "trim", "cross_source", "reorder"],
    transcript: [],
    sources: [],
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
}

describe("workflow to Proposal handoff", () => {
  it("runs unconfirmed draft → exact confirmed child → Proposal → roughcut handoff in order", async () => {
    const calls: string[] = [];
    const requestBodies: unknown[] = [];
    const exactReview = {
      ...review(),
      project: { ...review().project, revision: 5 },
    };
    const api: WorkflowApi = async <T = unknown>(
      path: string,
      init?: RequestInit,
    ): Promise<T> => {
      calls.push(path);
      if (init?.body !== undefined && typeof init.body === "string") {
        requestBodies.push(JSON.parse(init.body));
      }
      if (path.endsWith("content-draft-confirm")) {
        return {
          content_draft_mutation: {
            content_draft: {
              content_draft_id: "draft_confirmed",
              parent_draft_id: "draft_candidate",
              confirmed_by_user: true,
            },
            project_revision: 5,
          },
          workflow: {
            project: { revision: 5 },
            content_draft: {
              content_draft: {
                content_draft_id: "draft_confirmed",
                confirmed_by_user: true,
              },
            },
          },
        } as T;
      }
      return {
        content_draft_proposal: {
          content_draft_id: "draft_confirmed",
          proposal_schema_version: 2,
          proposal: exactReview.proposal,
          project_revision: 5,
        },
        review: exactReview,
      } as T;
    };

    await performWorkflowProposalHandoff(
      api,
      "draft_candidate",
      false,
      {
        operation_id: `dwop_1_${"1".repeat(32)}`,
        content_draft_id: "draft_candidate",
        expected_checkpoint_ref: {
          generation: 1,
          checkpoint_hash: "a".repeat(64),
        },
        expected_current_candidate_ref: {
          artifact_id: "draft_candidate",
          schema_version: 1,
          content_hash: "b".repeat(64),
        },
      },
      async (confirmedId, newlyConfirmed) => {
        calls.push(`confirmed:${confirmedId}:${newlyConfirmed}`);
      },
      async (handoff) => {
        calls.push(`roughcut:${handoff.review.basis.id}`);
      },
    );

    expect(calls).toEqual([
      "/api/workflow/content-draft-confirm",
      "confirmed:draft_confirmed:true",
      "/api/workflow/content-draft-propose",
      "roughcut:proposal_exact",
    ]);
    expect(requestBodies[0]).toEqual({
      operation_id: `dwop_1_${"1".repeat(32)}`,
      content_draft_id: "draft_candidate",
      expected_checkpoint_ref: {
        generation: 1,
        checkpoint_hash: "a".repeat(64),
      },
      expected_current_candidate_ref: {
        artifact_id: "draft_candidate",
        schema_version: 1,
        content_hash: "b".repeat(64),
      },
    });
  });

  it("reads the exact immutable confirmed child from the confirmation response", () => {
    const response = {
      content_draft_mutation: {
        content_draft: {
          content_draft_id: "draft_confirmed",
          parent_draft_id: "draft_candidate",
          confirmed_by_user: true,
        },
        project_revision: 5,
      },
      workflow: {
        project: { revision: 5 },
        content_draft: {
          content_draft: {
            content_draft_id: "draft_confirmed",
            confirmed_by_user: true,
          },
        },
      },
    };

    expect(parseConfirmedContentDraft(response, "draft_candidate")).toEqual({
      contentDraftId: "draft_confirmed",
      projectRevision: 5,
    });
    expect(() => parseConfirmedContentDraft(response, "draft_other")).toThrow(/初稿/);
    expect(() => parseConfirmedContentDraft({
      ...response,
      workflow: {
        ...response.workflow,
        content_draft: {
          content_draft: {
            content_draft_id: "draft_other",
            confirmed_by_user: true,
          },
        },
      },
    }, "draft_candidate")).toThrow(/确认结果/);
  });

  it("offers the action only for the exact active current confirmed fully recorded draft", () => {
    expect(workflowProposalEligibility(workflow(), false, false)).toEqual({
      visible: true,
      contentDraftId: "draft_confirmed",
    });
    expect(workflowProposalEligibility(workflow(), true, false).visible).toBe(false);
    expect(workflowProposalEligibility(workflow(), false, true).visible).toBe(false);
    expect(workflowProposalEligibility(workflow({
      allowed_operations: ["content_draft_read"],
    }), false, false).visible).toBe(false);
    expect(workflowProposalEligibility(workflow({
      session: { ...workflow().session, read_only: true, status: "stale" },
    }), false, false).visible).toBe(false);
    const unrecorded = workflow();
    unrecorded.content_draft!.content_draft.blocks = [{
      block_id: "narration_unrecorded",
      kind: "narration",
      text: "未录旁白",
      status: "approved",
      recorded_refs: [],
    }];
    expect(workflowProposalEligibility(unrecorded, false, false).visible).toBe(false);
  });

  it("hands off the exact server Review snapshot without rebuilding clips or IDs", () => {
    const exactReview = review();
    const response = {
      content_draft_proposal: {
        content_draft_id: "draft_confirmed",
        proposal_schema_version: 2,
        proposal: exactReview.proposal,
        project_revision: 4,
      },
      review: exactReview,
    };
    const handoff = parseWorkflowProposalHandoff(response, "draft_confirmed");
    expect(handoff.contentDraftId).toBe("draft_confirmed");
    expect(handoff.review).toBe(exactReview);
    expect(handoff.review.basis.id).toBe("proposal_exact");
    expect(handoff.review.proposal.clips[0]?.display_text).toBe("核心返回的原话");
  });

  it("accepts the exact schema-one roughcut handoff", () => {
    const exactReview = {
      ...review(),
      schema_version: 1 as const,
      proposal: {
        ...review().proposal,
        schema_version: 1 as const,
      },
    };
    const response = {
      content_draft_proposal: {
        content_draft_id: "draft_confirmed",
        proposal_schema_version: 1 as const,
        proposal: exactReview.proposal,
        project_revision: 4,
      },
      review: exactReview,
    };

    expect(
      parseWorkflowProposalHandoff(response, "draft_confirmed").review.schema_version,
    ).toBe(1);
  });

  it("rejects mismatched draft, Proposal, schema, revision, or non-Proposal handoff", () => {
    const exactReview = review();
    const base = {
      content_draft_proposal: {
        content_draft_id: "draft_confirmed",
        proposal_schema_version: 2,
        proposal: exactReview.proposal,
        project_revision: 4,
      },
      review: exactReview,
    };
    expect(() => parseWorkflowProposalHandoff(base, "draft_other")).toThrow(/初稿/);
    expect(() => parseWorkflowProposalHandoff({
      ...base,
      content_draft_proposal: {
        ...base.content_draft_proposal,
        proposal: { ...exactReview.proposal, proposal_id: "proposal_other" },
      },
    }, "draft_confirmed")).toThrow(/待确认剪辑方案/);
    expect(() => parseWorkflowProposalHandoff({
      ...base,
      content_draft_proposal: { ...base.content_draft_proposal, project_revision: 5 },
    }, "draft_confirmed")).toThrow(/项目内容已在生成期间变化/);
    expect(() => parseWorkflowProposalHandoff({
      ...base,
      review: { ...exactReview, basis: { type: "decision", id: "edit_x" } },
    }, "draft_confirmed")).toThrow(/待确认剪辑方案/);
  });
});
