import { describe, expect, it } from "vitest";

import {
  DraftEditorViewState,
  caretOffsetForParagraph,
  codePointToUtf16,
  contiguousPatch,
  optionalDisplayTitle,
  partitionText,
  sourceRangeForRun,
  type ActiveSelection,
  type EditorTextRange,
} from "./draft-editor-model";

describe("production draft editor view state", () => {
  it("keeps only the latest surface selection while preserving caret and search", () => {
    const state = new DraftEditorViewState("src_a");
    state.setCaret({
      candidate_id: "draft_a",
      paragraph_id: "paragraph_a",
      character_offset: 2,
      utf16_offset: 3,
      boundary_id: "caret_a",
      degraded: false,
      degradation_reason: null,
    });
    const draftSelection: ActiveSelection = {
      surface: "draft",
      request: {
        anchor: { paragraph_id: "paragraph_a", offset: 0, offset_encoding: "utf16" },
        focus: { paragraph_id: "paragraph_a", offset: 3, offset_encoding: "utf16" },
      },
      response: {
        candidate_id: "draft_a",
        surface: "draft",
        resolution: {
          direction: "forward",
          canonical_text: "你😀",
          refs: [],
          start_caret: null,
          end_caret: null,
          adjusted: false,
          degraded: false,
          degradation_reasons: [],
        },
        display_range: {
          anchor: { paragraph_id: "paragraph_a", character_offset: 0, utf16_offset: 0 },
          focus: { paragraph_id: "paragraph_a", character_offset: 2, utf16_offset: 3 },
        },
        correspondence_groups: [],
      },
      acceptedDegraded: false,
    };
    const sourceSelection: ActiveSelection = {
      ...draftSelection,
      surface: "source",
      response: {
        ...draftSelection.response,
        surface: "source",
        resolution: {
          ...draftSelection.response.resolution,
          canonical_text: "右侧原稿",
        },
      },
    };
    state.setSelection("draft", draftSelection);
    state.setSelection("source", sourceSelection);
    state.setSearch("draft", "重复", 1);
    state.selectSource("src_b");
    state.selectPane("source");

    expect(state.caret?.boundary_id).toBe("caret_a");
    expect(state.draftSelection).toBeNull();
    expect(state.sourceSelection?.response.resolution.canonical_text).toBe("右侧原稿");
    expect(state.search.draft).toEqual({ query: "重复", current: 1 });
    expect(state.currentSourceId).toBe("src_b");
    expect(state.mobilePane).toBe("source");
  });

  it("keeps a right selection when only the left caret changes", () => {
    const state = new DraftEditorViewState("src_a");
    const draftSelection: ActiveSelection = {
      surface: "draft",
      request: {
        anchor: { paragraph_id: "draft_a", offset: 0, offset_encoding: "utf16" },
        focus: { paragraph_id: "draft_a", offset: 2, offset_encoding: "utf16" },
      },
      response: {
        candidate_id: "candidate",
        surface: "draft",
        resolution: {
          direction: "forward",
          canonical_text: "左选区",
          refs: [],
          start_caret: null,
          end_caret: null,
          adjusted: false,
          degraded: false,
          degradation_reasons: [],
        },
        display_range: {
          anchor: { paragraph_id: "draft_a", character_offset: 0, utf16_offset: 0 },
          focus: { paragraph_id: "draft_a", character_offset: 2, utf16_offset: 2 },
        },
        correspondence_groups: [],
      },
      acceptedDegraded: true,
    };
    const sourceSelection: ActiveSelection = {
      ...draftSelection,
      surface: "source",
      request: {
        anchor: { paragraph_id: "source_a", offset: 1, offset_encoding: "utf16" },
        focus: { paragraph_id: "source_a", offset: 4, offset_encoding: "utf16" },
      },
      response: {
        ...draftSelection.response,
        surface: "source",
        resolution: {
          ...draftSelection.response.resolution,
          canonical_text: "右选区",
        },
        display_range: {
          anchor: { paragraph_id: "source_a", character_offset: 1, utf16_offset: 1 },
          focus: { paragraph_id: "source_a", character_offset: 4, utf16_offset: 4 },
        },
        correspondence_groups: [],
      },
    };
    state.setSelection("source", sourceSelection);
    state.setSourceLocation({
      sourceId: "src_a",
      paragraphId: "source_a",
    });
    state.setCaret({
      candidate_id: "candidate",
      paragraph_id: "draft_b",
      character_offset: 3,
      utf16_offset: 3,
      boundary_id: "caret_b",
      degraded: false,
      degradation_reason: null,
    });

    expect(state.draftSelection).toBeNull();
    expect(state.sourceSelection?.response.resolution.canonical_text).toBe("右选区");
    expect(state.caret?.boundary_id).toBe("caret_b");
    expect(state.sourceLocation).toEqual({
      sourceId: "src_a",
      paragraphId: "source_a",
    });
  });

  it("makes a new left or right selection globally exclusive", () => {
    const state = new DraftEditorViewState("src_a");
    const draftSelection = selection("draft", "左选区");
    const sourceSelection = selection("source", "右选区");

    state.setSelection("source", sourceSelection);
    state.setSelection("draft", draftSelection);
    expect(state.draftSelection?.response.resolution.canonical_text).toBe("左选区");
    expect(state.sourceSelection).toBeNull();

    state.setSelection("source", sourceSelection);
    expect(state.draftSelection).toBeNull();
    expect(state.sourceSelection?.response.resolution.canonical_text).toBe("右选区");
  });

  it("partitions Unicode text without losing repeated match identity", () => {
    const selected: EditorTextRange = {
      paragraphId: "paragraph_a",
      start: 1,
      end: 3,
    };
    const runs = partitionText("甲😀甲😀", "paragraph_a", {
      selected,
      matches: [
        { start: 0, end: 1, current: false },
        { start: 3, end: 4, current: true },
      ],
      correspondence: null,
      playback: null,
      caret: 3,
    });

    expect(runs.map((run) => run.text).join("")).toBe("甲😀甲😀");
    expect(runs.filter((run) => run.selected).map((run) => run.text).join("")).toBe("😀甲");
    expect(runs.filter((run) => run.currentMatch).map((run) => run.text).join("")).toBe("😀");
    expect(runs.filter((run) => run.caretBefore)).toHaveLength(1);
    expect(codePointToUtf16("甲😀乙", 2)).toBe(3);
  });

  it("renders a boundary caret in exactly one paragraph", () => {
    const caret = {
      candidate_id: "draft_a",
      paragraph_id: "paragraph_after",
      character_offset: 0,
      utf16_offset: 0,
      boundary_id: "shared_document_boundary",
      degraded: false,
      degradation_reason: null,
    };

    const before = partitionText("上一段", "paragraph_before", {
      selected: null,
      matches: [],
      correspondence: null,
      playback: null,
      caret: caretOffsetForParagraph(caret, "paragraph_before"),
    });
    const after = partitionText("下一段", "paragraph_after", {
      selected: null,
      matches: [],
      correspondence: null,
      playback: null,
      caret: caretOffsetForParagraph(caret, "paragraph_after"),
    });

    expect([...before, ...after].filter((run) => run.caretBefore)).toHaveLength(1);
    expect(after[0]?.caretBefore).toBe(true);

    const atDocumentEnd = partitionText("末段", "paragraph_after", {
      selected: null,
      matches: [],
      correspondence: null,
      playback: null,
      caret: 2,
    });
    expect(atDocumentEnd.filter((run) => run.caretBefore)).toHaveLength(1);
    expect(atDocumentEnd.at(-1)).toMatchObject({ text: "", start: 2, end: 2 });
  });

  it("uses source-document offsets rather than draft offsets for correspondence", () => {
    expect(sourceRangeForRun({
      source_id: "src_a",
      source_display_name: "素材 A",
      paragraph_id: "source_paragraph_a",
      start_ticks: 120_000,
      end_ticks: 240_000,
      start_offset: 20,
      end_offset: 24,
      source_start_offset: 3,
      source_end_offset: 7,
      text: "原话片段",
      refs: [],
    })).toEqual({
      paragraphId: "source_paragraph_a",
      start: 3,
      end: 7,
    });
  });

  it("plans a local paragraph patch around the changed manuscript range", () => {
    const signature = (value: { text: string }): string => value.text;
    expect(contiguousPatch(
      [{ text: "开头" }, { text: "删除我" }, { text: "结尾" }],
      [{ text: "开头" }, { text: "新内容" }, { text: "结尾" }],
      signature,
    )).toEqual({ prefixCount: 1, suffixCount: 1 });

    expect(contiguousPatch(
      [{ text: "开头" }, { text: "结尾" }],
      [{ text: "开头" }, { text: "加入" }, { text: "结尾" }],
      signature,
    )).toEqual({ prefixCount: 1, suffixCount: 1 });
  });

  it("uses only explicit draft and section titles, preserving legacy absence", () => {
    expect(optionalDisplayTitle("  校园精华  ")).toBe("校园精华");
    expect(optionalDisplayTitle(null)).toBeNull();
    expect(optionalDisplayTitle("   ")).toBeNull();
  });
});

function selection(
  surface: "draft" | "source",
  canonicalText: string,
): ActiveSelection {
  return {
    surface,
    request: {
      anchor: { paragraph_id: "paragraph", offset: 0, offset_encoding: "utf16" },
      focus: { paragraph_id: "paragraph", offset: 1, offset_encoding: "utf16" },
    },
    response: {
      candidate_id: "candidate",
      surface,
      resolution: {
        direction: "forward",
        canonical_text: canonicalText,
        refs: [],
        start_caret: null,
        end_caret: null,
        adjusted: false,
        degraded: false,
        degradation_reasons: [],
      },
      display_range: {
        anchor: { paragraph_id: "paragraph", character_offset: 0, utf16_offset: 0 },
        focus: { paragraph_id: "paragraph", character_offset: 1, utf16_offset: 1 },
      },
      correspondence_groups: [],
    },
    acceptedDegraded: true,
  };
}
