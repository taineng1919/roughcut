import { describe, expect, it } from "vitest";

import { visibleSelectionQuote } from "./draft-visible-selection";

describe("visible draft selection quote", () => {
  const paragraphs = [
    { paragraph_id: "first", text: "开头😀，" },
    { paragraph_id: "second", text: "第二段。" },
  ];

  it("uses the user's UTF-16 range rather than an expanded canonical segment", () => {
    expect(visibleSelectionQuote(paragraphs, {
      anchor: { paragraph_id: "first", offset: 2, offset_encoding: "utf16" },
      focus: { paragraph_id: "first", offset: 4, offset_encoding: "utf16" },
    })).toBe("😀");
  });

  it("preserves forward and reverse paragraph-spanning visible text", () => {
    const forward = {
      anchor: { paragraph_id: "first", offset: 0, offset_encoding: "utf16" as const },
      focus: { paragraph_id: "second", offset: 3, offset_encoding: "utf16" as const },
    };
    expect(visibleSelectionQuote(paragraphs, forward)).toBe("开头😀，\n第二段");
    expect(visibleSelectionQuote(paragraphs, {
      anchor: forward.focus,
      focus: forward.anchor,
    })).toBe("开头😀，\n第二段");
  });

  it("fails closed when the displayed candidate no longer contains an endpoint", () => {
    expect(visibleSelectionQuote(paragraphs, {
      anchor: { paragraph_id: "missing", offset: 0, offset_encoding: "utf16" },
      focus: { paragraph_id: "first", offset: 1, offset_encoding: "utf16" },
    })).toBeNull();
  });
});
