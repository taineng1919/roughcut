import { describe, expect, it } from "vitest";

import {
  DraftTranscriptWindowState,
  TRANSCRIPT_ROW_ESTIMATE,
  TRANSCRIPT_WINDOW_LIMIT,
} from "./draft-transcript-window";
import type { DraftTranscriptWindow, ReadableParagraph } from "./workflow-types";

function paragraph(index: number, sourceId = "src_a"): ReadableParagraph {
  return {
    paragraph_id: `${sourceId}_paragraph_${index}`,
    display_number: String(index + 1),
    source_id: sourceId,
    transcript_version_id: `${sourceId}_transcript`,
    source_display_name: sourceId,
    local_speaker_id: "spk_0",
    local_speaker_ids: ["spk_0"],
    person_id: null,
    person_name: null,
    text: index === 511 ? "跨窗口目标中文😀。" : `长稿段落 ${index} 重复窗口。`,
    start_ticks: index * 120_000,
    end_ticks: (index + 1) * 120_000,
    refs: [],
    adoption_status: "unadopted",
  };
}

function windowAt(offset: number, sourceId = "src_a"): DraftTranscriptWindow {
  const all = Array.from({ length: 540 }, (_, index) => paragraph(index, sourceId));
  return {
    candidate_id: "draft_a",
    source_id: sourceId,
    offset,
    limit: TRANSCRIPT_WINDOW_LIMIT,
    total: all.length,
    next_cursor: offset + TRANSCRIPT_WINDOW_LIMIT < all.length
      ? offset + TRANSCRIPT_WINDOW_LIMIT
      : null,
    previous_cursor: offset > 0 ? Math.max(0, offset - TRANSCRIPT_WINDOW_LIMIT) : null,
    located_paragraph_id: null,
    paragraphs: all.slice(offset, offset + TRANSCRIPT_WINDOW_LIMIT),
  };
}

describe("draft transcript viewport", () => {
  it("keeps a 500+ paragraph source to one mounted window", () => {
    const state = new DraftTranscriptWindowState("src_a");
    state.apply(windowAt(0));

    expect(state.total).toBe(540);
    expect(state.paragraphs).toHaveLength(TRANSCRIPT_WINDOW_LIMIT);
    expect(state.paragraphs.at(-1)?.paragraph_id).toBe("src_a_paragraph_47");
    expect(state.bottomSpacerHeight()).toBe(
      (540 - TRANSCRIPT_WINDOW_LIMIT) * TRANSCRIPT_ROW_ESTIMATE,
    );
  });

  it("switches windows without losing stable paragraph identity", () => {
    const state = new DraftTranscriptWindowState("src_a");
    state.apply(windowAt(0));
    const request = state.requestForScroll(500 * TRANSCRIPT_ROW_ESTIMATE);

    expect(request).toEqual({ sourceId: "src_a", offset: 476 });
    state.apply(windowAt(request!.offset));
    expect(state.paragraphs.some(
      (item) => item.paragraph_id === "src_a_paragraph_511"
        && item.text === "跨窗口目标中文😀。",
    )).toBe(true);
    expect(state.paragraphs).toHaveLength(TRANSCRIPT_WINDOW_LIMIT);
  });

  it("resets only the current window when switching material", () => {
    const state = new DraftTranscriptWindowState("src_a");
    state.apply(windowAt(240));
    expect(state.reset("src_b")).toEqual({ sourceId: "src_b", offset: 0 });
    expect(state.paragraphs).toEqual([]);
    expect(state.total).toBe(0);
  });

  it("extends only an adjacent window while a native source selection is active", () => {
    const state = new DraftTranscriptWindowState("src_a");
    state.apply(windowAt(48));
    state.extend(windowAt(0), "prepend");
    expect(state.offset).toBe(0);
    expect(state.paragraphs).toHaveLength(TRANSCRIPT_WINDOW_LIMIT * 2);
    expect(state.paragraphs[0]?.paragraph_id).toBe("src_a_paragraph_0");
    expect(state.paragraphs.at(-1)?.paragraph_id).toBe("src_a_paragraph_95");

    state.extend(windowAt(96), "append");
    expect(state.paragraphs).toHaveLength(TRANSCRIPT_WINDOW_LIMIT * 3);
    expect(state.paragraphs.at(-1)?.paragraph_id).toBe("src_a_paragraph_143");
    expect(state.bottomSpacerHeight()).toBe(
      (540 - TRANSCRIPT_WINDOW_LIMIT * 3) * TRANSCRIPT_ROW_ESTIMATE,
    );
  });

  it("requests an adjacent buffer instead of replacing the whole transcript", () => {
    const state = new DraftTranscriptWindowState("src_a");
    state.apply(windowAt(240));

    expect(state.requestForSelectionScroll(
      (240 + TRANSCRIPT_WINDOW_LIMIT - 2) * TRANSCRIPT_ROW_ESTIMATE,
    )).toEqual({
      direction: "append",
      request: { sourceId: "src_a", offset: 288 },
    });
    expect(state.requestForSelectionScroll(241 * TRANSCRIPT_ROW_ESTIMATE)).toEqual({
      direction: "prepend",
      request: { sourceId: "src_a", offset: 192 },
    });
  });
});
