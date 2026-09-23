import type { DraftEditorSurface } from "./draft-editor-model";

/** Keeps the click synthesized after a completed text drag from becoming a caret click. */
export class DraftPointerGesture {
  private selectionSurface: DraftEditorSurface | null = null;
  private phase: "idle" | "pressed" | "dragging" | "selecting" = "idle";
  private surface: DraftEditorSurface | null = null;
  private origin: { x: number; y: number } | null = null;
  private withinSelection = false;
  private clickedWithinSelection = false;
  private threshold = 4;

  pointerDown(
    surface: DraftEditorSurface,
    point: { x: number; y: number },
    options: { withinSelection?: boolean; threshold?: number } = {},
  ): "pressed" {
    this.phase = "pressed";
    this.surface = surface;
    this.origin = { ...point };
    this.withinSelection = options.withinSelection === true;
    this.clickedWithinSelection = false;
    this.threshold = options.threshold ?? 4;
    return "pressed";
  }

  pointerMove(point: { x: number; y: number }): "pressed" | "dragging" | "selecting" {
    if (this.phase === "idle" || this.origin === null) return "pressed";
    const distance = Math.hypot(point.x - this.origin.x, point.y - this.origin.y);
    if (this.phase === "pressed" && distance > this.threshold) {
      this.phase = this.withinSelection ? "dragging" : "selecting";
    }
    return this.phase;
  }

  pointerUp(hasTextSelection = false): "click" | "drag" | "select" {
    const result = this.phase === "dragging"
      ? "drag"
      : this.phase === "selecting"
        ? "select"
        : "click";
    this.clickedWithinSelection = result === "click" && this.withinSelection;
    const surface = this.surface;
    this.phase = "idle";
    this.surface = null;
    this.origin = null;
    this.withinSelection = false;
    if (result === "drag" || hasTextSelection) {
      this.selectionSurface = surface;
    }
    return result;
  }

  cancel(): void {
    this.phase = "idle";
    this.surface = null;
    this.origin = null;
    this.withinSelection = false;
    this.clickedWithinSelection = false;
  }

  state(): "idle" | "pressed" | "dragging" | "selecting" {
    return this.phase;
  }

  consumedSelectionClick(surface: DraftEditorSurface): boolean {
    void surface;
    const consumed = this.clickedWithinSelection;
    this.clickedWithinSelection = false;
    if (consumed) this.withinSelection = false;
    return consumed;
  }

  completePointerUp(surface: DraftEditorSurface, hasTextSelection: boolean): boolean {
    const insideSelectionClick = this.phase === "pressed" && this.withinSelection;
    if (insideSelectionClick) {
      this.clickedWithinSelection = true;
    }
    this.selectionSurface = hasTextSelection && !insideSelectionClick ? surface : null;
    return hasTextSelection && !insideSelectionClick;
  }

  consumesClick(surface: DraftEditorSurface): boolean {
    if (this.selectionSurface !== surface) return false;
    this.selectionSurface = null;
    return true;
  }
}
