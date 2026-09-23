import { describe, expect, it } from "vitest";

import {
  correctionPayload,
  displayedText,
  editReferenceMessage,
  normalizeCorrection,
  reviewSessionMessage,
} from "./transcript";
import type {
  ActiveTranscriptSegment,
  EditReferenceStatus,
  ReviewSessionState,
} from "./types";

const original: ActiveTranscriptSegment = {
  segment_id: "seg_1",
  start_ticks: 0,
  end_ticks: 120_000,
  original_text: "原始识别文字",
  corrected_text: null,
};

describe("transcript correction presentation", () => {
  it("keeps original text visible while selecting the actual displayed text", () => {
    expect(displayedText(original)).toEqual({
      text: "原始识别文字",
      status: "使用原文",
    });
    expect(displayedText({ ...original, corrected_text: "用户校正文字" })).toEqual({
      text: "用户校正文字",
      status: "已校正",
    });
  });

  it("trims sparse corrections, rejects blanks, and uses null for restore", () => {
    expect(normalizeCorrection("  新文字  ")).toBe("新文字");
    expect(() => normalizeCorrection("   ")).toThrow("校正文不能为空");
    expect(correctionPayload("seg_1", "  新文字  ")).toEqual({
      segment_id: "seg_1",
      corrected_text: "新文字",
    });
    expect(correctionPayload("seg_1", null)).toEqual({
      segment_id: "seg_1",
      corrected_text: null,
    });
  });
});

describe("stale transcript state", () => {
  it("names only actual Edit mismatches", () => {
    const status: EditReferenceStatus = {
      status: "stale",
      mismatches: [
        {
          source_id: "src_a",
          referenced_transcript_version_id: "tr_a",
          active_transcript_version_id: "tr_a_child",
        },
      ],
    };
    expect(editReferenceMessage(status, new Map([
      ["src_a", "素材 A"],
      ["src_b", "素材 B"],
    ]))).toContain("素材 A");
    expect(editReferenceMessage(status, new Map([
      ["src_a", "素材 A"],
      ["src_b", "素材 B"],
    ]))).not.toContain("素材 B");
  });

  it("locks the old Review even after the Edit returns current", () => {
    const session: ReviewSessionState = {
      status: "stale",
      read_only: true,
      snapshot_revision: 3,
      current_revision: 5,
      mismatches: [],
    };
    expect(reviewSessionMessage(session)).toContain("只读");
    expect(reviewSessionMessage(session)).toContain("重新打开当前步骤");
  });
});
