import { describe, expect, it } from "vitest";

import {
  DraftWorkspaceMutationRetry,
  locksDraftWorkspaceWrites,
} from "./workflow";
import type { DraftWorkspaceMutationBasis } from "./workflow-types";

function basis(generation = 7): DraftWorkspaceMutationBasis {
  return {
    expected_checkpoint_ref: {
      generation,
      checkpoint_hash: "a".repeat(64),
    },
    expected_current_candidate_ref: {
      artifact_id: "draft_current",
      schema_version: 1,
      content_hash: "b".repeat(64),
    },
  };
}

function retry(): DraftWorkspaceMutationRetry {
  let sequence = 0;
  return new DraftWorkspaceMutationRetry(
    (generation) => `dwop_${generation}_${(++sequence).toString().padStart(32, "0")}`,
  );
}

describe("Draft Workspace mutation retry identity", () => {
  it("reuses an undo operation ID only for the same action, input, and basis", () => {
    const current = retry();
    const first = current.mutation("draft-undo", { candidate_id: "draft_current" }, basis());
    const repeated = current.mutation("draft-undo", { candidate_id: "draft_current" }, basis());

    expect(repeated.operation_id).toBe(first.operation_id);
  });

  it("uses different IDs for undo and redo with the same candidate", () => {
    const current = retry();
    const undo = current.mutation("draft-undo", { candidate_id: "draft_current" }, basis());
    const redo = current.mutation("draft-redo", { candidate_id: "draft_current" }, basis());

    expect(redo.operation_id).not.toBe(undo.operation_id);
  });

  it("distinguishes delete, move, and insert", () => {
    const current = retry();
    const input = { candidate_id: "draft_current", selection: "same" };
    const ids = new Set([
      current.mutation("draft-edit:delete", input, basis()).operation_id,
      current.mutation("draft-edit:move", input, basis()).operation_id,
      current.mutation("draft-edit:insert", input, basis()).operation_id,
    ]);

    expect(ids.size).toBe(3);
  });

  it("uses a new ID when the business input or checkpoint basis changes", () => {
    const current = retry();
    const first = current.mutation("draft-narration", {
      candidate_id: "draft_current",
      text: "第一版",
    }, basis());
    const changedInput = current.mutation("draft-narration", {
      candidate_id: "draft_current",
      text: "第二版",
    }, basis());
    const changedBasis = current.mutation("draft-narration", {
      candidate_id: "draft_current",
      text: "第二版",
    }, basis(8));

    expect(changedInput.operation_id).not.toBe(first.operation_id);
    expect(changedBasis.operation_id).not.toBe(changedInput.operation_id);
  });

  it("clears the old retry identity after a successful snapshot applies", () => {
    const current = retry();
    const before = current.mutation("approve-draft", {
      content_draft_id: "draft_current",
    }, basis());
    current.clear();
    const after = current.mutation("approve-draft", {
      content_draft_id: "draft_next",
    }, basis(8));

    expect(after.operation_id).not.toBe(before.operation_id);
    expect(after.operation_id).toMatch(/^dwop_8_/);
  });

  it("keeps stale and action-conflict responses write-locking", () => {
    expect(locksDraftWorkspaceWrites({ code: "draft_workspace_stale" })).toBe(true);
    expect(locksDraftWorkspaceWrites({ code: "draft_workspace_action_conflict" })).toBe(true);
    expect(locksDraftWorkspaceWrites({ code: "workflow_required" })).toBe(false);
  });
});
