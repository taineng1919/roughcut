import { describe, expect, it } from "vitest";

import { endpointFromDom } from "./draft-dom-endpoint";

type FakeNode = {
  nodeType: number;
  parentElement: FakeElement | null;
  parentNode: FakeElement | null;
  childNodes: FakeNode[];
  textContent: string | null;
};

type FakeElement = FakeNode & {
  dataset: Record<string, string | undefined>;
  matches: (selector: string) => boolean;
  closest: (selector: string) => FakeElement | null;
};

const ELEMENT = 1;
const TEXT = 3;

function text(value: string): FakeNode {
  return {
    nodeType: TEXT,
    parentElement: null,
    parentNode: null,
    childNodes: [],
    textContent: value,
  };
}

function element(
  attributes: Record<string, string> = {},
  children: FakeNode[] = [],
): FakeElement {
  const node: FakeElement = {
    nodeType: ELEMENT,
    parentElement: null,
    parentNode: null,
    childNodes: children,
    textContent: null,
    dataset: attributes,
    matches(selector: string): boolean {
      return (selector === "[data-text-fragment]" && this.dataset.textFragment === "true")
        || (selector === "[data-editor-text]" && this.dataset.editorText === "true")
        || (selector === "[data-editor-paragraph]" && this.dataset.editorParagraph !== undefined);
    },
    closest(selector: string): FakeElement | null {
      let current: FakeElement | null = this;
      while (current !== null) {
        if (current.matches(selector)) return current;
        current = current.parentElement;
      }
      return null;
    },
  };
  for (const child of children) {
    child.parentElement = node;
    child.parentNode = node;
  }
  node.textContent = children.map((child) => child.textContent ?? "").join("");
  return node;
}

function paragraphFixture(): {
  paragraph: FakeElement;
  editorText: FakeElement;
  firstText: FakeNode;
  secondText: FakeNode;
  caret: FakeElement;
} {
  const firstText = text("你好，");
  const secondText = text("😀世界。");
  const first = element({ textFragment: "true", start: "0", utf16Start: "0" }, [firstText]);
  const caret = element({ insertionCaret: "true" });
  const second = element({ textFragment: "true", start: "3", utf16Start: "3" }, [secondText]);
  const editorText = element({ editorText: "true" }, [first, second, caret]);
  const byline = element();
  const paragraph = element({ editorParagraph: "paragraph_1", surface: "draft" }, [byline, editorText]);
  return { paragraph, editorText, firstText, secondText, caret };
}

describe("draft DOM endpoint normalization", () => {
  it("normalizes start, end-after-punctuation, and full-paragraph child boundaries", () => {
    const fixture = paragraphFixture();

    expect(endpointFromDom(fixture.editorText as unknown as Node, 0, "draft")).toEqual({
      paragraph_id: "paragraph_1", offset: 0, offset_encoding: "utf16",
    });
    for (let attempt = 0; attempt < 10; attempt += 1) {
      const start = endpointFromDom(fixture.editorText as unknown as Node, 0, "draft");
      const end = endpointFromDom(
        fixture.editorText as unknown as Node,
        fixture.editorText.childNodes.length,
        "draft",
      );
      expect(start).toEqual({ paragraph_id: "paragraph_1", offset: 0, offset_encoding: "utf16" });
      expect(end).toEqual({ paragraph_id: "paragraph_1", offset: 8, offset_encoding: "utf16" });
      expect({ anchor: end, focus: start }).toEqual({
        anchor: { paragraph_id: "paragraph_1", offset: 8, offset_encoding: "utf16" },
        focus: { paragraph_id: "paragraph_1", offset: 0, offset_encoding: "utf16" },
      });
    }
    expect(endpointFromDom(fixture.paragraph as unknown as Node, 0, "draft")).toEqual({
      paragraph_id: "paragraph_1", offset: 0, offset_encoding: "utf16",
    });
    expect(endpointFromDom(fixture.paragraph as unknown as Node, 2, "draft")).toEqual({
      paragraph_id: "paragraph_1", offset: 8, offset_encoding: "utf16",
    });
  });

  it("keeps first-punctuation, UTF-16, adjacent fragments, and final punctuation boundaries deterministic", () => {
    const fixture = paragraphFixture();

    expect(endpointFromDom(fixture.firstText as unknown as Node, 3, "draft")).toEqual({
      paragraph_id: "paragraph_1", offset: 3, offset_encoding: "utf16",
    });
    expect(endpointFromDom(fixture.editorText as unknown as Node, 1, "draft")).toEqual({
      paragraph_id: "paragraph_1", offset: 3, offset_encoding: "utf16",
    });
    expect(endpointFromDom(fixture.secondText as unknown as Node, 2, "draft")).toEqual({
      paragraph_id: "paragraph_1", offset: 5, offset_encoding: "utf16",
    });
    expect(endpointFromDom(fixture.secondText as unknown as Node, 4, "draft")).toEqual({
      paragraph_id: "paragraph_1", offset: 7, offset_encoding: "utf16",
    });
    expect(endpointFromDom(fixture.secondText as unknown as Node, 5, "draft")).toEqual({
      paragraph_id: "paragraph_1", offset: 8, offset_encoding: "utf16",
    });
  });

  it("rejects visual carets, decorations, and an endpoint from another surface", () => {
    const fixture = paragraphFixture();
    const decoration = element({}, [text("装饰")]);
    const insertionCaret = element();
    decoration.parentElement = fixture.paragraph;
    insertionCaret.parentElement = fixture.editorText;
    fixture.editorText.childNodes.push(insertionCaret);

    expect(endpointFromDom(fixture.caret as unknown as Node, 0, "draft")).toBeNull();
    expect(endpointFromDom(insertionCaret as unknown as Node, 0, "draft")).toBeNull();
    expect(endpointFromDom(decoration as unknown as Node, 0, "draft")).toBeNull();
    expect(endpointFromDom(fixture.editorText as unknown as Node, 0, "source")).toBeNull();
  });
});
