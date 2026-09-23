import { describe, expect, it } from "vitest";

import { WorkflowDraftEditor, draftActionState } from "./workflow-draft";
import type {
  ContentDraftBlock,
  SelectionResolution,
  WorkflowReviewPayload,
} from "./workflow-types";

const sourceBlock: ContentDraftBlock = {
  block_id: "block_source",
  kind: "source_excerpt",
  canonical_text: "来源原话",
  refs: [{
    source_id: "src_a",
    transcript_version_id: "tr_a",
    segment_id: "seg_a",
    start_ticks: 0,
    end_ticks: 120_000,
  }],
};
const narrationBlock: ContentDraftBlock = {
  block_id: "block_narration",
  kind: "narration",
  text: "旁白文字",
  status: "draft",
  recorded_refs: [],
};

describe("workflow Content Draft editor", () => {
  it("deletes, restores and reorders only local copied blocks", () => {
    const editor = new WorkflowDraftEditor([sourceBlock, narrationBlock], true, "draft_parent");
    editor.delete("block_source");
    expect(editor.blocks.map((block) => block.block_id)).toEqual(["block_narration"]);
    expect(editor.restoreLastDeleted()).toBe(true);
    expect(editor.blocks.map((block) => block.block_id)).toEqual([
      "block_source",
      "block_narration",
    ]);
    editor.move(1, 0);
    expect(editor.blocks[0]?.block_id).toBe("block_narration");
    expect(editor.displayNumber("block_narration")).toBe("D002");
    expect(editor.displayNumber("block_source")).toBe("D001");
    expect([sourceBlock, narrationBlock].map((block) => block.block_id)).toEqual([
      "block_source",
      "block_narration",
    ]);
  });

  it("allows narration editing but keeps source canonical text read-only", () => {
    const editor = new WorkflowDraftEditor([sourceBlock, narrationBlock], true, null);
    editor.editNarration("block_narration", "  新旁白  ");
    expect(editor.blocks[1]).toMatchObject({ text: "新旁白", status: "draft" });
    expect(() => editor.editNarration("block_source", "改写原话")).toThrow("来源原话只读");
    editor.addNarration("block_new", "新增旁白", 1);
    expect(editor.blocks[1]).toMatchObject({
      block_id: "block_new",
      kind: "narration",
      status: "draft",
    });
    expect(editor.displayNumber("block_new")).toBe("D003");
    expect(editor.hasUnrecordedNarration).toBe(true);
  });

  it("invalidates approval and recorded refs when narration text changes", () => {
    const recorded: ContentDraftBlock = {
      ...narrationBlock,
      status: "recorded",
      recorded_refs: sourceBlock.refs,
    };
    const editor = new WorkflowDraftEditor([sourceBlock, recorded], true, null);
    editor.editNarration("block_narration", "新旁白");
    expect(editor.blocks[1]).toMatchObject({
      text: "新旁白",
      status: "draft",
      recorded_refs: [],
    });
  });

  it("inserts exact resolved refs at a specified D position and saves a full candidate", () => {
    const resolution: SelectionResolution = {
      view_hash: "a".repeat(64),
      mode: "exact",
      canonical_text: "选中的原话",
      warnings: [],
      refs: [{
        source_id: "src_b",
        transcript_version_id: "tr_b",
        segment_id: "seg_b",
        start_ticks: 240_000,
        end_ticks: 360_000,
        canonical_text: "选中的原话",
      }],
    };
    const editor = new WorkflowDraftEditor([sourceBlock, narrationBlock], true, "draft_parent");
    editor.insertResolution("block_selection", resolution, 1);

    expect(editor.blocks.map((block) => block.block_id)).toEqual([
      "block_source",
      "block_selection",
      "block_narration",
    ]);
    expect(editor.candidatePayload()).toEqual({
      parent_draft_id: "draft_parent",
      blocks: editor.blocks,
    });
  });

  it("blocks reorder when the frozen Brief forbids it", () => {
    const editor = new WorkflowDraftEditor([sourceBlock, narrationBlock], false, null);
    expect(() => editor.move(1, 0)).toThrow("当前剪辑要求需要尽量保持原顺序");
  });

  it("keeps candidate save, draft confirmation and Proposal as separate UI actions", () => {
    const editor = new WorkflowDraftEditor([sourceBlock, narrationBlock], true, "draft_candidate");
    const snapshot = {
      allowed_operations: ["content_draft_create", "content_draft_confirm"],
      content_draft: {
        content_draft: {
          content_draft_id: "draft_candidate",
          confirmed_by_user: false,
        },
      },
      session: { read_only: false },
    } as WorkflowReviewPayload;
    expect(draftActionState(editor, snapshot)).toEqual({
      canSaveCandidate: false,
      canConfirmDraft: true,
      showProposalAction: false,
    });
    editor.editNarration("block_narration", "本地修改");
    expect(draftActionState(editor, snapshot)).toEqual({
      canSaveCandidate: true,
      canConfirmDraft: false,
      showProposalAction: false,
    });
  });

  it("does not offer candidate persistence before a Brief enables draft creation", () => {
    const editor = new WorkflowDraftEditor([sourceBlock], true, null);
    editor.addNarration("block_new", "本地旁白", 1);
    const snapshot = {
      allowed_operations: ["brief_create", "readable_transcript_read"],
      content_draft: null,
      session: { read_only: false },
    } as WorkflowReviewPayload;
    expect(draftActionState(editor, snapshot).canSaveCandidate).toBe(false);
  });
});
