import { describe, expect, it } from "vitest";

import { DraftPointerGesture } from "./draft-pointer-gesture";

describe("draft pointer gesture", () => {
  it("suppresses the click after each completed text drag", () => {
    const gesture = new DraftPointerGesture();

    for (let attempt = 0; attempt < 10; attempt += 1) {
      expect(gesture.completePointerUp("draft", true)).toBe(true);
      expect(gesture.consumesClick("draft")).toBe(true);
      expect(gesture.consumesClick("draft")).toBe(false);
    }
  });

  it("leaves a genuine collapsed click available for caret placement", () => {
    const gesture = new DraftPointerGesture();

    expect(gesture.completePointerUp("draft", false)).toBe(false);
    expect(gesture.consumesClick("draft")).toBe(false);
  });

  it("uses the Word-style threshold without timers for inside and outside presses", () => {
    const gesture = new DraftPointerGesture();

    gesture.pointerDown("draft", { x: 0, y: 0 }, { withinSelection: true });
    expect(gesture.pointerMove({ x: 3, y: 0 })).toBe("pressed");
    expect(gesture.pointerMove({ x: 5, y: 0 })).toBe("dragging");
    expect(gesture.pointerUp()).toBe("drag");

    gesture.pointerDown("draft", { x: 0, y: 0 }, { withinSelection: false });
    expect(gesture.pointerMove({ x: 5, y: 0 })).toBe("selecting");
    expect(gesture.pointerUp()).toBe("select");
  });

  it("marks an inside click so the old selection can be cancelled", () => {
    const gesture = new DraftPointerGesture();
    gesture.pointerDown("draft", { x: 0, y: 0 }, { withinSelection: true });
    expect(gesture.pointerUp()).toBe("click");
    expect(gesture.consumedSelectionClick("draft")).toBe(true);
    expect(gesture.consumedSelectionClick("draft")).toBe(false);
  });

  it("does not re-resolve a stale native range on an inside click", () => {
    const gesture = new DraftPointerGesture();
    gesture.pointerDown("draft", { x: 0, y: 0 }, { withinSelection: true });
    expect(gesture.completePointerUp("draft", true)).toBe(false);
    expect(gesture.consumedSelectionClick("draft")).toBe(true);
  });
});
