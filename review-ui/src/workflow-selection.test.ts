import { describe, expect, it } from "vitest";

import {
  SelectionPayloadCache,
  selectionPayloadFromUtf16,
  selectionResolutionAction,
  utf16OffsetToCodePoint,
  validateSelectionParagraph,
} from "./workflow-selection";

describe("workflow Unicode selection mapping", () => {
  it("converts DOM UTF-16 offsets around emoji to Unicode code-point offsets", () => {
    const text = "你好😀世界😀";
    expect(utf16OffsetToCodePoint(text, 4)).toBe(3);
    expect(selectionPayloadFromUtf16("paragraph_1", text, 2, 4)).toEqual({
      paragraph_id: "paragraph_1",
      start_offset: 2,
      end_offset: 3,
      quote: "😀",
      occurrence: 0,
    });
    expect(selectionPayloadFromUtf16("paragraph_1", text, 6, 8).occurrence).toBe(1);
  });

  it("keeps Chinese and combining marks as explicit code points", () => {
    expect(selectionPayloadFromUtf16("paragraph_2", "中e\u0301文", 1, 3)).toMatchObject({
      start_offset: 1,
      end_offset: 3,
      quote: "e\u0301",
    });
  });

  it("rejects surrogate splits, empty/reversed ranges and mismatched paragraphs", () => {
    expect(() => utf16OffsetToCodePoint("A😀B", 2)).toThrow("代理对中间");
    expect(() => selectionPayloadFromUtf16("paragraph_1", "文字", 1, 1)).toThrow("正向非空");
    expect(() => selectionPayloadFromUtf16("paragraph_1", "文字", 2, 1)).toThrow("正向非空");
    expect(() => validateSelectionParagraph("paragraph_1", "paragraph_2")).toThrow("同一个原始转录段落");
    expect(() => validateSelectionParagraph(null, "paragraph_1")).toThrow("同一个原始转录段落");
  });

  it("requires explicit acceptance when the core expands to complete segments", () => {
    expect(selectionResolutionAction("exact")).toBe("insert");
    expect(selectionResolutionAction("expanded_to_segments")).toBe("confirm_expansion");
  });

  it("preserves the exact pointer-down selection across button focus and consumes it once", () => {
    const cache = new SelectionPayloadCache();
    const payload = selectionPayloadFromUtf16(
      "paragraph_repeat",
      "重复😀文字。重复😀文字。",
      8,
      14,
    );
    cache.capture(payload);
    expect(cache.consume("paragraph_repeat")).toEqual(payload);
    expect(cache.consume("paragraph_repeat")).toBeNull();
    cache.capture(payload);
    expect(() => cache.consume("paragraph_other")).toThrow("原始转录段落");
  });
});
