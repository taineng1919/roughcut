import { describe, expect, it } from "vitest";

import { partitionText } from "./draft-editor-model";
import {
  appendDraftTextRuns,
  narrationStatusLabel,
  visibleBylineForSurface,
} from "./workflow";

type FakeElement = {
  tagName: string;
  className: string;
  dataset: Record<string, string | undefined>;
  textContent: string | null;
  children: FakeElement[];
  attributes: Record<string, string>;
  append: (...children: FakeElement[]) => void;
  setAttribute: (name: string, value: string) => void;
};

function element(tagName: string): FakeElement {
  return {
    tagName,
    className: "",
    dataset: {},
    textContent: null,
    children: [],
    attributes: {},
    append(...children: FakeElement[]): void {
      this.children.push(...children);
    },
    setAttribute(name: string, value: string): void {
      this.attributes[name] = value;
    },
  };
}

describe("draft text DOM", () => {
  it("hides draft bylines while keeping source bylines visible", () => {
    const person = { name: "人物甲", role: "主持人", local_speaker_id: "spk_0" };
    expect(visibleBylineForSurface("draft", person)).toBeNull();
    expect(visibleBylineForSurface("source", person)).toBe("人物甲 · 主持人");
  });

  it("keeps narration role and recording state labels explicit", () => {
    expect(narrationStatusLabel("draft")).toBe("待录音");
    expect(narrationStatusLabel("recorded")).toBe("已绑定录音");
  });

  it("renders source-run fragments without inline caret buttons", () => {
    const value = "第一句。第二句！第三句。";
    const target = element("SPAN");
    const runs = partitionText(value, "paragraph_1", {
      selected: null,
      matches: [],
      correspondence: null,
      playback: null,
      caret: Array.from(value).length,
      boundaries: [3, 7],
    });
    const createCaret = (): HTMLElement => {
      const caret = element("SPAN");
      caret.className = "draft-insertion-caret";
      caret.setAttribute("aria-hidden", "true");
      return caret as unknown as HTMLElement;
    };

    appendDraftTextRuns(
      target as unknown as HTMLElement,
      value,
      runs,
      createCaret,
      (tagName) => element(tagName.toUpperCase()) as unknown as HTMLElement,
    );

    const rendered = target.children;
    expect(rendered).toHaveLength(4);
    expect(rendered.map((child) => child.tagName)).toEqual(["SPAN", "SPAN", "SPAN", "SPAN"]);
    expect(rendered.some((child) => child.tagName === "BUTTON")).toBe(false);
    expect(rendered.filter((child) => child.dataset.textFragment === "true")).toHaveLength(3);
    expect(rendered.at(-1)?.attributes["aria-hidden"]).toBe("true");
  });
});
