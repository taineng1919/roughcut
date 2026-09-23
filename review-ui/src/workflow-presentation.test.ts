import { describe, expect, it } from "vitest";

import {
  TranscriptFilterState,
  adoptionText,
  draftAdoptionForParagraph,
  draftManuscript,
  filterParagraphsByDraftAdoption,
  matchingParagraphIds,
  playbackTargetForBlock,
  playbackTargetForParagraph,
} from "./workflow-presentation";
import type {
  ContentDraftBlock,
  ReadableParagraph,
  WorkflowFilters,
} from "./workflow-types";

const blocks: ContentDraftBlock[] = [
  {
    block_id: "block_a_1",
    kind: "source_excerpt",
    canonical_text: "重复原话。",
    refs: [{
      source_id: "src_a",
      transcript_version_id: "tr_a",
      segment_id: "seg_a_1",
      start_ticks: 120_000,
      end_ticks: 240_000,
    }],
  },
  {
    block_id: "block_a_2",
    kind: "source_excerpt",
    canonical_text: "同一部分的下一段。",
    refs: [{
      source_id: "src_a",
      transcript_version_id: "tr_a",
      segment_id: "seg_a_2",
      start_ticks: 240_000,
      end_ticks: 360_000,
    }],
  },
  {
    block_id: "block_voice",
    kind: "narration",
    text: "这里是解说。",
    status: "approved",
    recorded_refs: [],
  },
  {
    block_id: "block_b",
    kind: "source_excerpt",
    canonical_text: "重复原话。",
    refs: [{
      source_id: "src_b",
      transcript_version_id: "tr_b",
      segment_id: "seg_b_1",
      start_ticks: 600_000,
      end_ticks: 720_000,
    }],
  },
];

function paragraph(
  paragraphId: string,
  sourceId: string,
  transcriptId: string,
  segmentId: string,
  text: string,
): ReadableParagraph {
  return {
    paragraph_id: paragraphId,
    display_number: "P001",
    source_id: sourceId,
    transcript_version_id: transcriptId,
    source_display_name: sourceId === "src_a" ? "素材甲" : "素材乙",
    local_speaker_id: "spk_0",
    local_speaker_ids: ["spk_0"],
    person_id: null,
    person_name: null,
    text,
    start_ticks: sourceId === "src_a" ? 120_000 : 600_000,
    end_ticks: sourceId === "src_a" ? 240_000 : 720_000,
    refs: [{
      source_id: sourceId,
      transcript_version_id: transcriptId,
      segment_id: segmentId,
      start_ticks: sourceId === "src_a" ? 120_000 : 600_000,
      end_ticks: sourceId === "src_a" ? 240_000 : 720_000,
    }],
    adoption_status: "unadopted",
  };
}

describe("workflow manuscript presentation", () => {
  it("groups consecutive source paragraphs and narration into readable sections", () => {
    const manuscript = draftManuscript(blocks, (sourceId) => ({
      src_a: "素材甲",
      src_b: "素材乙",
    })[sourceId] ?? sourceId);

    expect(manuscript.map((section) => section.title)).toEqual([
      "素材甲",
      "解说",
      "素材乙",
    ]);
    expect(manuscript[0]?.paragraphs.map((item) => item.text)).toEqual([
      "重复原话。",
      "同一部分的下一段。",
    ]);
    expect(JSON.stringify(manuscript)).not.toContain("D001");
    expect(JSON.stringify(manuscript)).not.toContain("Content Draft");
  });

  it("uses exact refs rather than repeated text for two-way location and playback", () => {
    const paragraphs = [
      paragraph("paragraph_a", "src_a", "tr_a", "seg_a_1", "重复原话。"),
      paragraph("paragraph_b", "src_b", "tr_b", "seg_b_1", "重复原话。"),
    ];

    expect(matchingParagraphIds(blocks[0]!, paragraphs)).toEqual(["paragraph_a"]);
    expect(matchingParagraphIds(blocks[3]!, paragraphs)).toEqual(["paragraph_b"]);
    expect(playbackTargetForBlock(blocks[3]!)).toEqual({
      sourceId: "src_b",
      ticks: 600_000,
      segmentId: "seg_b_1",
    });
    expect(playbackTargetForParagraph(paragraphs[0]!)).toEqual({
      sourceId: "src_a",
      ticks: 120_000,
      paragraphId: "paragraph_a",
    });
  });

  it("uses ordinary adoption wording", () => {
    expect(adoptionText("adopted")).toBe("已采用");
    expect(adoptionText("partial")).toBe("部分采用");
    expect(adoptionText("unadopted")).toBe("未采用");
    expect(adoptionText("not_applicable")).toBe("暂无剪辑方案");
  });

  it("derives initial-draft adoption from exact identity and half-open ticks", () => {
    const original = paragraph("paragraph_a", "src_a", "tr_a", "seg_a_1", "重复原话。");
    original.adoption_status = "not_applicable";
    expect(draftAdoptionForParagraph(original, [blocks[0]!])).toBe("adopted");
    const partial = structuredClone(blocks[0]!);
    if (partial.kind !== "source_excerpt") throw new Error("fixture kind changed");
    partial.refs[0]!.end_ticks = 180_000;
    expect(draftAdoptionForParagraph(original, [partial])).toBe("partial");
    expect(draftAdoptionForParagraph(original, [blocks[3]!])).toBe("unadopted");
  });

  it("filters initial-draft adoption by exact refs even when repeated text is identical", () => {
    const originalA = paragraph("paragraph_a", "src_a", "tr_a", "seg_a_1", "重复原话。");
    const originalB = paragraph("paragraph_b", "src_b", "tr_b", "seg_b_1", "重复原话。");
    originalA.adoption_status = "not_applicable";
    originalB.adoption_status = "not_applicable";

    expect(filterParagraphsByDraftAdoption(
      [originalA, originalB],
      [blocks[0]!],
      ["adopted"],
    ).map((item) => item.paragraph_id)).toEqual(["paragraph_a"]);
    expect(filterParagraphsByDraftAdoption(
      [originalA, originalB],
      [blocks[0]!],
      ["unadopted"],
    ).map((item) => item.paragraph_id)).toEqual(["paragraph_b"]);
  });
});

describe("original transcript filter state", () => {
  it("distinguishes pending, applied and cleared filters without implying playback", () => {
    const state = new TranscriptFilterState();
    expect(state.message(224)).toBe("未使用筛选，当前显示 224 段原始转录稿。");
    const selected: WorkflowFilters = { source_ids: ["src_a"], keyword: "课程" };
    state.edit(selected);
    expect(state.message(224)).toContain("筛选条件尚未应用");
    expect(state.message(224)).toContain("不会播放素材或修改初稿");
    state.apply(selected);
    expect(state.message(2)).toBe("筛选已应用，当前显示 2 段原始转录稿。");
    state.clear();
    expect(state.message(224)).toBe("未使用筛选，当前显示 224 段原始转录稿。");
  });
});
