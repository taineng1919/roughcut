import { afterEach, describe, expect, it, vi } from "vitest";

import {
  endpointFromRenderedFragments,
  WorkflowReviewController,
} from "./workflow";
import { DraftEditorViewState, type ActiveSelection } from "./draft-editor-model";
import type { DraftEditServerTiming } from "./draft-edit-performance";
import type {
  DraftEditorParagraph,
  DraftEditorRequestPoint,
  DraftEditorSnapshot,
  DraftWorkspaceMutationBasis,
  WorkflowApi,
} from "./workflow-types";

type Rect = { left: number; right: number; top: number; bottom: number; width: number; height: number };

const text = "甲😀乙宽窄";
const boundaries = [0, 1, 3, 4, 5, 6];

function rectFor(offset: number): Rect {
  return { left: offset * 10, right: offset * 10, top: 0, bottom: 10, width: 0, height: 10 };
}

function installEndpointDocument(options: { trustedRanges?: boolean } = {}): void {
  const textNode = { nodeType: 3, textContent: text } as unknown as Text;
  let paragraph: HTMLElement;
  const fragment = {
    dataset: { textFragment: "true", utf16Start: "0" },
    textContent: text,
    getClientRects: () => [{ ...rectFor(0), right: 60, width: 60 }],
    getBoundingClientRect: () => ({ ...rectFor(0), right: 60, width: 60 }),
    firstChild: textNode,
    childNodes: [textNode],
    closest: () => paragraph,
  } as unknown as HTMLElement;
  paragraph = {
    dataset: { surface: "draft", editorParagraph: "paragraph_a" },
    closest: () => paragraph,
    querySelectorAll: () => [fragment],
  } as unknown as HTMLElement;
  const range = {
    offset: 0,
    setStart(this: Range & { offset: number }, _node: Node, offset: number): void {
      this.offset = offset;
    },
    collapse(): void {},
    getBoundingClientRect(this: Range & { offset: number }): Rect {
      return options.trustedRanges === false
        ? { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 }
        : rectFor(this.offset);
    },
  } as unknown as Range & { offset: number };
  vi.stubGlobal("document", {
    elementsFromPoint: () => [fragment],
    createRange: () => range,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("rendered draft target resolution", () => {
  it("uses the clicked visual line instead of the first fragment rect", () => {
    installEndpointDocument();
    const point = endpointFromRenderedFragments(16, 5, "draft");
    expect(point?.paragraph_id).toBe("paragraph_a");
    expect(point?.offset).toBeGreaterThanOrEqual(1);
  });

  it("keeps mixed-width and emoji clicks on real UTF-16 boundaries", () => {
    installEndpointDocument();
    const point = endpointFromRenderedFragments(15, 5, "draft");
    expect(boundaries).toContain(point?.offset);
    expect(point?.offset).not.toBe(2);
  });

  it("returns no target when the paragraph has no trusted boundary geometry", () => {
    installEndpointDocument({ trustedRanges: false });
    expect(endpointFromRenderedFragments(16, 5, "draft")).toBeNull();
  });
});

describe("direct drag controller", () => {
  it("moves left content with one schema-2 POST and keeps pointermove write-free", async () => {
    const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const api: WorkflowApi = async <T>(path: string, options?: RequestInit) => {
      if (typeof options?.body === "string") calls.push({ path, body: JSON.parse(options.body) });
      return { draft_editor: snapshot, timing: serverTiming() } as T;
    };
    const controller = dragController(snapshot, api);
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    harness.view.setSelection("draft", targetSelection());
    dom.surface = "draft";
    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(12, 5));
    expect(calls).toHaveLength(0);
    dom.surface = "draft";
    harness.handleDragPointerMove(pointer(55, 5));
    expect(calls).toHaveLength(0);
    await harness.handleDragPointerUp(pointer(55, 5));
    expect(calls).toHaveLength(1);
    expect(calls[0]?.path).toBe("/api/workflow/draft-edit");
    expect(calls[0]?.body.operation).toBe("move_selection");
    expect(calls[0]?.body.schema_version).toBe(2);
    expect(calls[0]?.body.operation_id).toMatch(/^dwop_1_/);
    expect((calls[0]?.body.target as Record<string, unknown>).utf16_offset).toBe(5);
    expect(dom.shell.dataset).toMatchObject({
      editOperation: "move",
      editSelectionCaretRevalidationMs: "1",
      editImmutableChildWriteFsyncMs: "2",
      editProjectBriefTranscriptContextValidationMs: "3",
      editWorkflowSnapshotRefreshMs: "4",
      editDraftSnapshotRebuildMs: "5",
      editServerBeforeResponseMs: "6",
    });
  });

  it("captures the business pointer only after the threshold and releases it on drop", async () => {
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const controller = dragController(snapshot, async <T>() => ({ draft_editor: snapshot } as unknown as T));
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    const target = {
      setPointerCapture: vi.fn(),
      hasPointerCapture: vi.fn(() => true),
      releasePointerCapture: vi.fn(),
    } as unknown as HTMLElement;
    harness.view.setSelection("draft", targetSelection());
    dom.surface = "draft";
    harness.handleDragPointerDown("draft", pointer(10, 5, 7));
    harness.draftDrag!.pointerTarget = target;
    harness.draftDrag!.pointerId = 7;
    harness.handleDragPointerMove(pointer(55, 5, 7));
    expect(target.setPointerCapture).toHaveBeenCalledWith(7);
    await harness.handleDragPointerUp(pointer(55, 5, 7));
    expect(target.hasPointerCapture).toHaveBeenCalledWith(7);
    expect(target.releasePointerCapture).toHaveBeenCalledWith(7);
  });

  it("inserts exact source refs into the left pane without changing the source selection", async () => {
    const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const api: WorkflowApi = async <T>(path: string, options?: RequestInit) => {
      if (typeof options?.body === "string") calls.push({ path, body: JSON.parse(options.body) });
      return { draft_editor: snapshot } as T;
    };
    const controller = dragController(snapshot, api);
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    harness.view.setSelection("source", sourceSelection());
    dom.surface = "source";
    harness.handleDragPointerDown("source", pointer(10, 5));
    dom.surface = "draft";
    harness.handleDragPointerMove(pointer(55, 5));
    await harness.handleDragPointerUp(pointer(55, 5));
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body.operation).toBe("insert_source_refs");
    expect((calls[0]?.body.source as Record<string, unknown>).kind).toBe("exact_source_refs");
  });

  it("cancels below threshold, with Escape, outside the selection, and on a return to the selection", async () => {
    const calls: string[] = [];
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const api: WorkflowApi = async <T>(path: string) => {
      calls.push(path);
      return { draft_editor: snapshot } as T;
    };
    const controller = dragController(snapshot, api);
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    dom.surface = "draft";
    harness.handleDragPointerDown("draft", pointer(10, 5));
    await harness.handleDragPointerUp(pointer(10, 5));
    expect(calls).toHaveLength(0);

    harness.view.setSelection("draft", targetSelection());
    harness.handleDragPointerDown("draft", pointer(10, 5));
    await harness.handleShortcut({
      key: "Escape",
      target: null,
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
    } as unknown as KeyboardEvent);
    expect(calls).toHaveLength(0);

    harness.handleDragPointerDown("draft", pointer(10, 5));
    dom.surface = "draft";
    harness.handleDragPointerMove(pointer(20, 5));
    await harness.handleDragPointerUp(pointer(20, 5));
    expect(calls).toHaveLength(0);

    dom.hit = "outside";
    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(55, 5));
    await harness.handleDragPointerUp(pointer(55, 5));
    expect(calls).toHaveLength(0);
  });

  it("updates the exact UTF-16 indicator locally and auto-scrolls without an API call", () => {
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const controller = dragController(snapshot, async <T>() => ({ draft_editor: snapshot } as unknown as T));
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    harness.view.setSelection("draft", targetSelection());
    dom.surface = "draft";
    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(55, 200));
    expect(dom.insertedOffset).toBeNull();
    expect(dom.indicatorCreates).toBe(1);
    expect(dom.host.scrollTop).toBeGreaterThan(0);
  });

  it("reuses the indicator when the target is unchanged and never normalizes text", () => {
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const controller = dragController(snapshot, async <T>() => ({ draft_editor: snapshot } as unknown as T));
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    harness.view.setSelection("draft", targetSelection());
    dom.surface = "draft";
    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(55, 5));
    harness.handleDragPointerMove(pointer(55, 5));
    expect(dom.indicatorCreates).toBe(1);
    expect(dom.normalizeCalls).toBe(0);
  });

  it("starts a section drag only from the dedicated handle", () => {
    const snapshot = dragSnapshot();
    const controller = dragController(snapshot, async <T>() => ({ draft_editor: snapshot } as unknown as T));
    const harness = controller as unknown as DragHarness;
    harness.view.setDraftMode("sections");
    const row = { dataset: { headingBlockId: "heading_a", sectionRow: "true" } } as unknown as HTMLElement;
    const handle = sectionTarget(row, true);
    const title = sectionTarget(row, false);
    harness.handleSectionPointerDown(pointerEvent(title));
    expect(harness.sectionDrag).toBeNull();
    harness.handleSectionPointerDown(pointerEvent(handle));
    expect(harness.sectionDrag?.headingId).toBe("heading_a");
  });

  it("clears stale client drag samples before every non-drag section operation", async () => {
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const calls: string[] = [];
    const controller = dragController(snapshot, async <T>(_path: string, options?: RequestInit) => {
      calls.push(JSON.parse(String(options?.body)).operation as string);
      return { draft_editor: snapshot, timing: serverTiming() } as T;
    });
    const harness = controller as unknown as DragHarness;
    harness.renderSections = () => {};
    for (const operation of [
      "section_rename",
      "section_split",
      "section_merge",
      "section_delete",
    ] as const) {
      Object.assign(dom.shell.dataset, {
        editSelectionResolveMs: "11",
        editPointermoveHitTestP95Ms: "12",
        editPointermoveHitTestSamples: "30",
        editDropHttpServerMs: "13",
        editDomPatchMs: "14",
      });
      await harness.applySectionOperation(operation, {});
      expect(dom.shell.dataset).not.toHaveProperty("editSelectionResolveMs");
      expect(dom.shell.dataset).not.toHaveProperty("editPointermoveHitTestP95Ms");
      expect(dom.shell.dataset).not.toHaveProperty("editPointermoveHitTestSamples");
      expect(dom.shell.dataset).not.toHaveProperty("editDropHttpServerMs");
      expect(dom.shell.dataset).not.toHaveProperty("editDomPatchMs");
      expect(dom.shell.dataset).toMatchObject({
        editOperation: operation,
        editSelectionCaretRevalidationMs: "1",
        editImmutableChildWriteFsyncMs: "2",
        editProjectBriefTranscriptContextValidationMs: "3",
        editWorkflowSnapshotRefreshMs: "4",
        editDraftSnapshotRebuildMs: "5",
        editServerBeforeResponseMs: "6",
      });
    }
    expect(calls).toEqual([
      "section_rename",
      "section_split",
      "section_merge",
      "section_delete",
    ]);
  });

  it("refreshes the visible section projection immediately after shortcut undo and redo", async () => {
    const initial = sectionHistorySnapshot(
      dragSnapshot(),
      "candidate_a",
      1,
      [
        ["heading_a", "第一章"],
        ["heading_b", "第二章"],
      ],
      true,
      false,
    );
    const undone = sectionHistorySnapshot(
      initial,
      "candidate_b",
      2,
      [
        ["heading_b", "第二章（撤销后）"],
        ["heading_a", "第一章"],
      ],
      true,
      true,
    );
    const redone = sectionHistorySnapshot(
      undone,
      "candidate_c",
      3,
      [
        ["heading_a", "第一章"],
        ["heading_b", "第二章"],
      ],
      true,
      false,
    );
    const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
    const api: WorkflowApi = async <T>(path: string, options?: RequestInit) => {
      calls.push({ path, body: JSON.parse(String(options?.body)) });
      return { draft_editor: path.endsWith("draft-undo") ? undone : redone } as T;
    };
    const controller = dragController(initial, api);
    const harness = controller as unknown as DragHarness;
    const sections = { hidden: false, titles: [] as string[] };
    harness.view.setDraftMode("sections");
    harness.applySnapshot = (next) => {
      harness.snapshot = next;
    };
    harness.renderSections = () => {
      sections.titles = (harness.snapshot.blocks ?? [])
        .filter((block) => block.kind === "section_title")
        .map((block) => block.title);
    };
    harness.renderCorrespondenceNavigation = () => {};
    harness.renderPlayer = () => {};
    harness.refreshSearches = async () => {};
    harness.renderSections();

    await harness.handleShortcut(shortcut("z"));

    expect(calls).toHaveLength(1);
    expect(calls[0]?.path).toBe("/api/workflow/draft-undo");
    expect(calls[0]?.body.expected_checkpoint_ref).toEqual(
      initial.workspace.expected_checkpoint_ref,
    );
    expect(harness.snapshot.candidate.candidate_id).toBe("candidate_b");
    expect(harness.snapshot.workspace.expected_checkpoint_ref.generation).toBe(2);
    expect(harness.snapshot.history).toEqual(undone.history);
    expect(harness.view.draftMode).toBe("sections");
    expect(sections.hidden).toBe(false);
    expect(sections.titles).toEqual(["第二章（撤销后）", "第一章"]);

    await harness.handleShortcut(shortcut("z", true));

    expect(calls).toHaveLength(2);
    expect(calls[1]?.path).toBe("/api/workflow/draft-redo");
    expect(calls[1]?.body.expected_checkpoint_ref).toEqual(
      undone.workspace.expected_checkpoint_ref,
    );
    expect(harness.snapshot.candidate.candidate_id).toBe("candidate_c");
    expect(harness.snapshot.workspace.expected_checkpoint_ref.generation).toBe(3);
    expect(harness.snapshot.history).toEqual(redone.history);
    expect(harness.view.draftMode).toBe("sections");
    expect(sections.hidden).toBe(false);
    expect(sections.titles).toEqual(["第一章", "第二章"]);
  });

  it("keeps body mode while applying a successful history snapshot", async () => {
    const initial = dragSnapshot();
    const undone = nextDragSnapshot(initial, "candidate_b", 2, "c", "d");
    const calls: string[] = [];
    const controller = dragController(initial, async <T>(path: string) => {
      calls.push(path);
      return { draft_editor: undone } as T;
    });
    const harness = controller as unknown as DragHarness;
    const applied: DraftEditorSnapshot[] = [];
    harness.view.setDraftMode("body");
    harness.applySnapshot = (next) => {
      applied.push(next);
      harness.snapshot = next;
    };
    harness.renderSections = () => {};
    harness.renderCorrespondenceNavigation = () => {};
    harness.renderPlayer = () => {};
    harness.refreshSearches = async () => {};

    await harness.handleShortcut(shortcut("z"));

    expect(calls).toEqual(["/api/workflow/draft-undo"]);
    expect(applied).toEqual([undone]);
    expect(harness.snapshot.candidate.candidate_id).toBe("candidate_b");
    expect(harness.view.draftMode).toBe("body");
  });

  it("ignores a stale response after the drag generation is cancelled", async () => {
    const pending = deferred<{ draft_editor: DraftEditorSnapshot }>();
    const calls: string[] = [];
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const api: WorkflowApi = async <T>(path: string) => {
      calls.push(path);
      return pending.promise as T;
    };
    const controller = dragController(snapshot, api);
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    let applied = 0;
    harness.applySnapshot = () => { applied += 1; };
    harness.view.setSelection("draft", targetSelection());
    dom.surface = "draft";
    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(55, 5));
    const request = harness.handleDragPointerUp(pointer(55, 5));
    await Promise.resolve();
    expect(calls).toEqual(["/api/workflow/draft-edit"]);
    harness.draftDragGeneration += 1;
    pending.resolve({ draft_editor: snapshot });
    await request;
    expect(applied).toBe(0);
  });

  it("does not surface a stale failed response after the drag generation is cancelled", async () => {
    const pending = deferred<{ draft_editor: DraftEditorSnapshot }>();
    const calls: string[] = [];
    const snapshot = dragSnapshot();
    const dom = installDragDocument();
    const statuses: string[] = [];
    void pending.promise.catch(() => {});
    const api: WorkflowApi = async <T>(path: string) => {
      calls.push(path);
      return pending.promise as T;
    };
    const controller = dragController(snapshot, api, (message) => statuses.push(message));
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    harness.view.setSelection("draft", targetSelection());
    dom.surface = "draft";
    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(55, 5));
    const request = harness.handleDragPointerUp(pointer(55, 5));
    await Promise.resolve();
    expect(calls).toEqual(["/api/workflow/draft-edit"]);
    harness.draftDragGeneration += 1;
    pending.reject({ code: "draft_edit_failed", message: "stale" });
    await request;
    expect(statuses).toEqual([]);
  });

  it("keeps Escape current, applies the successful snapshot, then drops from the new basis", async () => {
    const pending = deferred<{ draft_editor: DraftEditorSnapshot }>();
    const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
    const snapshot = dragSnapshot();
    const updated = nextDragSnapshot(snapshot, "candidate_b", 2, "c", "d");
    const twiceUpdated = nextDragSnapshot(updated, "candidate_c", 3, "e", "f");
    const dom = installDragDocument();
    const api: WorkflowApi = async <T>(path: string, options?: RequestInit) => {
      if (typeof options?.body === "string") calls.push({ path, body: JSON.parse(options.body) });
      return (calls.length === 1
        ? pending.promise
        : Promise.resolve({ draft_editor: twiceUpdated })) as T;
    };
    const controller = dragController(snapshot, api);
    const harness = controller as unknown as DragHarness;
    harness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    const applied: DraftEditorSnapshot[] = [];
    harness.applySnapshot = (value) => {
      applied.push(value);
      harness.snapshot = value;
    };
    harness.view.setSelection("draft", targetSelection());
    dom.surface = "draft";
    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(55, 5));
    const request = harness.handleDragPointerUp(pointer(55, 5));
    await Promise.resolve();
    expect(calls).toHaveLength(1);
    const generation = harness.draftDragGeneration;
    harness.cancelDrag();
    await harness.handleShortcut({
      key: "Escape",
      target: null,
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent);
    expect(harness.draftDragGeneration).toBe(generation);
    expect(harness.busy).toBe(true);
    expect(harness.pendingDraftDrop).not.toBeNull();

    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(55, 5));
    await harness.handleDragPointerUp(pointer(55, 5));
    expect(calls).toHaveLength(1);

    pending.resolve({ draft_editor: updated });
    await request;
    expect(applied).toEqual([updated]);
    expect(harness.busy).toBe(false);
    expect(harness.pendingDraftDrop).toBeNull();

    harness.view.setSelection("draft", targetSelection("candidate_b"));
    harness.handleDragPointerDown("draft", pointer(10, 5));
    harness.handleDragPointerMove(pointer(55, 5));
    await harness.handleDragPointerUp(pointer(55, 5));
    expect(calls).toHaveLength(2);
    expect(calls[1]?.body.expected_checkpoint_ref).toEqual(updated.workspace.expected_checkpoint_ref);
    expect(calls[1]?.body.expected_current_candidate_ref).toEqual(
      updated.workspace.expected_current_candidate_ref,
    );
    expect(calls[1]?.body.operation_id).toMatch(/^dwop_2_/);
    expect(applied).toEqual([updated, twiceUpdated]);
  });

  it("keeps invalid drops retryable while stale and unknown failures lock closed", async () => {
    const snapshot = dragSnapshot();
    const explicit = installDragDocument();
    const statuses: string[] = [];
    let attempts = 0;
    const explicitApi: WorkflowApi = async <T>() => {
      attempts += 1;
      if (attempts === 1) {
        throw { code: "invalid_workflow_change", message: "落点无效" };
      }
      return { draft_editor: snapshot } as T;
    };
    const explicitController = dragController(
      snapshot,
      explicitApi,
      (message) => statuses.push(message),
    );
    const explicitHarness = explicitController as unknown as DragHarness;
    explicitHarness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    explicitHarness.view.setSelection("draft", targetSelection());
    explicit.surface = "draft";
    explicitHarness.handleDragPointerDown("draft", pointer(10, 5));
    explicitHarness.handleDragPointerMove(pointer(55, 5));
    await explicitHarness.handleDragPointerUp(pointer(55, 5));
    expect(explicitHarness.busy).toBe(false);
    expect(explicitHarness.pendingDraftDrop).toBeNull();
    expect(explicitHarness.view.locked).toBe(false);
    expect(statuses).toEqual([
      "这个落点没有产生有效调整，内容未更改；请拖到另一处再试",
    ]);

    explicitHarness.view.setSelection("draft", targetSelection());
    explicit.surface = "draft";
    explicitHarness.handleDragPointerDown("draft", pointer(10, 5));
    explicitHarness.handleDragPointerMove(pointer(55, 5));
    await explicitHarness.handleDragPointerUp(pointer(55, 5));
    expect(attempts).toBe(2);
    expect(explicitHarness.busy).toBe(false);
    expect(explicitHarness.view.locked).toBe(false);

    const stale = installDragDocument();
    const staleApi: WorkflowApi = async () => {
      throw { code: "draft_workspace_stale", message: "旧 checkpoint" };
    };
    const staleController = dragController(snapshot, staleApi);
    const staleHarness = staleController as unknown as DragHarness;
    staleHarness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    staleHarness.view.setSelection("draft", targetSelection());
    stale.surface = "draft";
    staleHarness.handleDragPointerDown("draft", pointer(10, 5));
    staleHarness.handleDragPointerMove(pointer(55, 5));
    await staleHarness.handleDragPointerUp(pointer(55, 5));
    expect(staleHarness.busy).toBe(false);
    expect(staleHarness.pendingDraftDrop).toBeNull();
    expect(staleHarness.view.locked).toBe(true);

    const uncertain = installDragDocument();
    const uncertainApi: WorkflowApi = async () => {
      throw new Error("network status unknown");
    };
    const uncertainController = dragController(snapshot, uncertainApi);
    const uncertainHarness = uncertainController as unknown as DragHarness;
    uncertainHarness.displayPointInSelection = (...args: unknown[]) =>
      (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
    uncertainHarness.view.setSelection("draft", targetSelection());
    uncertain.surface = "draft";
    uncertainHarness.handleDragPointerDown("draft", pointer(10, 5));
    uncertainHarness.handleDragPointerMove(pointer(55, 5));
    await uncertainHarness.handleDragPointerUp(pointer(55, 5));
    expect(uncertainHarness.busy).toBe(false);
    expect(uncertainHarness.pendingDraftDrop).toBeNull();
    expect(uncertainHarness.view.locked).toBe(true);
  });

  it.each([
    "draft_workspace_stale",
    "draft_workspace_action_conflict",
    "stale_review",
  ])("renders %s as a stale lock", (code) => {
    const dom = installDragDocument();
    const controller = dragController(dragSnapshot(), async <T>() => ({}) as T);
    const harness = controller as unknown as DragHarness;
    harness.handleError({ code, message: "stale" });
    harness.renderConfirm();
    expect(harness.view.locked).toBe(true);
    expect(dom.banner.hidden).toBe(false);
    expect(dom.bannerTitle.textContent).toBe("项目内容已在别处更新");
  });

  it("keeps an uncertain lock distinct from stale after a later fetch failure", () => {
    const dom = installDragDocument();
    const controller = dragController(dragSnapshot(), async <T>() => ({}) as T);
    const harness = controller as unknown as DragHarness;
    harness.lock("uncertain");
    harness.handleError(new TypeError("Failed to fetch"));
    harness.renderConfirm();
    expect(harness.view.locked).toBe(true);
    expect(dom.banner.hidden).toBe(false);
    expect(dom.bannerTitle.textContent).toBe("拖放请求状态不明确");
    expect(dom.bannerCopy.textContent).toContain("请求是否已生效");
  });

  it("classifies punctuation ambiguity and out-of-bounds drops distinctly, keeping the selection retryable", async () => {
    const snapshot = dragSnapshot();
    for (const [message, expected] of [
      ["draft editor selection cannot uniquely retain display punctuation", "当前选区两侧的人工标点无法唯一保留；请把相关标点一并选中，或先调整标点后再移动。"],
      ["draft editor canonical offset is out of bounds", "落点超出有效范围，内容未更改；请拖到另一处再试。"],
      ["draft editor display offset is out of bounds", "落点超出有效范围，内容未更改；请拖到另一处再试。"],
      ["draft editor text offset is out of bounds", "落点超出有效范围，内容未更改；请拖到另一处再试。"],
      ["落点无效", "这个落点没有产生有效调整，内容未更改；请拖到另一处再试"],
    ] as const) {
      const dom = installDragDocument();
      const statuses: string[] = [];
      const api: WorkflowApi = async () => {
        throw { code: "invalid_workflow_change", message };
      };
      const controller = dragController(snapshot, api, (status) => statuses.push(status));
      const harness = controller as unknown as DragHarness;
      harness.displayPointInSelection = (...args: unknown[]) =>
        (args[1] as DraftEditorRequestPoint).paragraph_id !== "paragraph_b";
      harness.view.setSelection("draft", targetSelection());
      dom.surface = "draft";
      harness.handleDragPointerDown("draft", pointer(10, 5));
      harness.handleDragPointerMove(pointer(55, 5));
      await harness.handleDragPointerUp(pointer(55, 5));
      expect(statuses).toEqual([expected]);
      expect(harness.busy).toBe(false);
      expect(harness.view.locked).toBe(false);
      expect(harness.view.draftSelection).not.toBeNull();
    }
  });

  it("freezes selection endpoints before invalidating during capture", async () => {
    const dom = installDragDocument();
    const captured: Array<{ anchor: string; focus: string }> = [];
    let reads = 0;
    const api: WorkflowApi = async <T>(path: string, options?: RequestInit) => {
      if (path.endsWith("draft-selection-resolve") && typeof options?.body === "string") {
        const body = JSON.parse(options.body) as { anchor: { paragraph_id: string; offset: number }; focus: { paragraph_id: string; offset: number } };
        captured.push({ anchor: `${body.anchor.paragraph_id}:${body.anchor.offset}`, focus: `${body.focus.paragraph_id}:${body.focus.offset}` });
      }
      return { candidate_id: "candidate_a", paragraphs: [], workspace: {} } as unknown as T;
    };
    const controller = dragController(dragSnapshot(), api);
    const harness = controller as unknown as DragHarness;
    const order: string[] = [];
    harness.invalidateCaretResolve = () => { order.push("invalidate"); };
    harness.patchParagraphs = () => { order.push("patch"); };
    harness.renderInteractionState = () => {};
    harness.renderCorrespondenceNavigation = () => {};
    harness.renderPlayer = () => {};
    harness.renderToolbar = () => {};
    void dom;
    const paragraphStub = {
      dataset: { surface: "draft", editorParagraph: "paragraph_a", paragraphKind: "source_excerpt" },
      querySelectorAll: () => [],
    };
    const fragmentStub = {
      dataset: { textFragment: "true", utf16Start: "0" },
      closest: (selector: string) => selector === "[data-editor-paragraph]"
        ? paragraphStub
        : selector === "[data-text-fragment]"
          ? fragmentStub
          : null,
    };
    const endpointNode = {
      nodeType: 3,
      textContent: "甲😀乙宽窄",
      parentElement: fragmentStub,
    };
    vi.stubGlobal("document", {
      getElementById: (id: string) => id === "draft-source-document"
        ? { removeAttribute: () => { order.push("removeBusy"); } }
        : id === "draft-editor-shell"
          ? { dataset: {} }
          : null,
      elementsFromPoint: () => [],
      createRange: () => ({ setStart() {}, collapse() {}, getBoundingClientRect: () => ({ left: 0, right: 0, top: 0, bottom: 0 }) }),
      querySelectorAll: () => [],
    });
    vi.stubGlobal("window", {
      getSelection: () => {
        reads += 1;
        if (reads === 1) {
          return {
            isCollapsed: false,
            anchorNode: endpointNode,
            anchorOffset: 1,
            focusNode: endpointNode,
            focusOffset: 3,
            removeAllRanges: () => { order.push("removeAllRanges"); },
          };
        }
        // After the first read the DOM is invalidated: a second read must not happen.
        return null;
      },
    });
    await (harness as unknown as { captureSelection: (surface: "draft" | "source") => Promise<void> }).captureSelection("draft");
    expect(captured).toEqual([{ anchor: "paragraph_a:1", focus: "paragraph_a:3" }]);
    expect(reads).toBe(1);
    expect(order).toContain("invalidate");
    expect(order.indexOf("invalidate")).toBeLessThan(order.indexOf("patch"));
  });
});

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((complete, fail) => {
    resolve = complete;
    reject = fail;
  });
  return { promise, resolve, reject };
}

type DragHarness = {
  view: DraftEditorViewState;
  snapshot: DraftEditorSnapshot;
  handleDragPointerDown: (surface: "draft" | "source", event: PointerEvent) => void;
  handleDragPointerMove: (event: PointerEvent) => void;
  handleDragPointerUp: (event: PointerEvent) => Promise<void>;
  handleDragPointerCancel: (event: PointerEvent) => void;
  cancelDrag: () => void;
  handleShortcut: (event: KeyboardEvent) => Promise<void>;
  displayPointInSelection: (...args: unknown[]) => boolean;
  applySnapshot: (snapshot: DraftEditorSnapshot) => void;
  patchParagraphs: (...args: unknown[]) => void;
  invalidateCaretResolve: () => void;
  renderInteractionState: () => void;
  renderToolbar: () => void;
  busy: boolean;
  pendingDraftDrop: unknown;
  draftDragGeneration: number;
  draftDrag: {
    pointerTarget: HTMLElement | null;
    pointerId: number | null;
  } | null;
  capturePointer: (target: HTMLElement | null, pointerId: number | null) => void;
  releasePointer: (target: HTMLElement | null, pointerId: number | null) => void;
  setBusinessDragState: (active: boolean) => void;
  handleSectionPointerDown: (event: PointerEvent) => void;
  applySectionOperation: (
    operation: "section_rename" | "section_split" | "section_merge" | "section_delete",
    payload: Record<string, unknown>,
  ) => Promise<void>;
  renderSections: () => void;
  renderCorrespondenceNavigation: () => void;
  renderPlayer: () => void;
  refreshSearches: () => Promise<void>;
  sectionDrag: { headingId: string } | null;
  handleError: (error: unknown) => void;
  lock: (reason: "stale" | "uncertain") => void;
  renderConfirm: () => void;
};

function dragController(
  snapshot: DraftEditorSnapshot,
  api: WorkflowApi,
  onStatus: (message: string, warning?: boolean) => void = () => {},
): WorkflowReviewController {
  const controller = new WorkflowReviewController(snapshot, api, onStatus, async () => {});
  const harness = controller as unknown as DragHarness;
  harness.renderInteractionState = () => {};
  harness.applySnapshot = () => {};
  harness.patchParagraphs = () => {};
  harness.renderCorrespondenceNavigation = () => {};
  harness.renderPlayer = () => {};
  return controller;
}

function pointer(x: number, y: number, pointerId = 1): PointerEvent {
  return { clientX: x, clientY: y, pointerId, preventDefault(): void {} } as PointerEvent;
}

function installDragDocument(): {
  host: HTMLElement & { scrollTop: number };
  shell: HTMLElement;
  banner: HTMLElement & { hidden: boolean };
  bannerTitle: HTMLElement;
  bannerCopy: HTMLElement;
  surface: "draft" | "source";
  hit: "inside" | "outside";
  insertedOffset: number | null;
  insertedOffsets: number[];
  indicatorCreates: number;
  normalizeCalls: number;
} {
  const state = {
    surface: "draft" as "draft" | "source",
    hit: "inside" as "inside" | "outside",
    insertedOffset: null as number | null,
    insertedOffsets: [] as number[],
    indicatorCreates: 0,
    normalizeCalls: 0,
  };
  const host = {
    dataset: {},
    scrollTop: 0,
    getBoundingClientRect: () => ({ top: 0, bottom: 100, left: 0, right: 100, width: 100, height: 100 }),
    append: (indicator: HTMLElement) => {
      const item = indicator as HTMLElement & { remove: () => void; parentElement: HTMLElement };
      item.parentElement = host as unknown as HTMLElement;
      item.remove = () => {
        const index = indicators.indexOf(item);
        if (index >= 0) indicators.splice(index, 1);
      };
      indicators.push(item);
    },
    querySelectorAll: (selector: string) => selector.includes("data-editor-paragraph")
      ? [draftSourceRow, sourceRow, targetRow]
      : selector.includes("data-draft-drop-indicator")
        ? indicators
        : selector.includes("data-drop-target")
          ? [targetRow].filter((row) => row.dataset.dropTarget !== undefined)
          : [],
  } as unknown as HTMLElement & { scrollTop: number };
  const indicators: Array<HTMLElement & { remove: () => void; parentElement: HTMLElement }> = [];
  const sourceTextNode = { nodeType: 3, textContent: text } as unknown as Text;
  const targetTextNode = { nodeType: 3, textContent: "目标文本宽窄" } as unknown as Text;
  const sourceFragment = fragment("source_a", sourceTextNode, 0);
  const draftSourceFragment = fragment("paragraph_a", sourceTextNode, 0);
  const targetFragment = fragment("paragraph_b", targetTextNode, 0);
  const mutableTargetFragment = targetFragment as unknown as MutableFragment;
  mutableTargetFragment.childNodes = [targetTextNode];
  mutableTargetFragment.firstChild = targetTextNode;
  mutableTargetFragment.normalize = () => {
    state.normalizeCalls += 1;
    const value = mutableTargetFragment.childNodes
      .filter((node) => node.nodeType === 3)
      .map((node) => node.textContent ?? "")
      .join("");
    const merged = { nodeType: 3, textContent: value } as unknown as Text;
    mutableTargetFragment.childNodes = [merged];
    mutableTargetFragment.firstChild = merged;
    mutableTargetFragment.textContent = value;
  };
  sourceRow = row("source_a", "source", sourceFragment);
  draftSourceRow = row("paragraph_a", "draft", draftSourceFragment);
  targetRow = row("paragraph_b", "draft", targetFragment);
  const range = {
    offset: 0,
    setStart(this: { offset: number }, _node: Node, offset: number): void { this.offset = offset; },
    collapse(): void {},
    getBoundingClientRect(this: { offset: number }): Rect { return rectFor(this.offset); },
    insertNode(this: { offset: number }, indicator: HTMLElement): void {
      state.insertedOffset = this.offset;
      state.insertedOffsets.push(this.offset);
      const item = indicator as HTMLElement & { remove: () => void; parentElement: HTMLElement };
      const fragment = mutableTargetFragment;
      const nodeIndex = fragment.childNodes.indexOf(targetTextNode);
      const node = nodeIndex >= 0 ? targetTextNode : fragment.firstChild;
      const value = node?.textContent ?? "";
      const before = { nodeType: 3, textContent: value.slice(0, this.offset) } as unknown as Text;
      const after = { nodeType: 3, textContent: value.slice(this.offset) } as unknown as Text;
      const insertIndex = nodeIndex >= 0 ? nodeIndex : 0;
      fragment.childNodes.splice(insertIndex, 1, before, item, after);
      fragment.firstChild = before;
      fragment.textContent = fragment.childNodes
        .map((child) => child.nodeType === 3 ? child.textContent ?? "" : "")
        .join("");
      item.parentElement = fragment as unknown as HTMLElement;
      item.remove = () => {
        const index = indicators.indexOf(item);
        if (index >= 0) indicators.splice(index, 1);
        const childIndex = fragment.childNodes.indexOf(item);
        if (childIndex >= 0) fragment.childNodes.splice(childIndex, 1);
      };
      indicators.push(item);
    },
  } as unknown as Range & { offset: number };
  const shell = {
    dataset: {},
    classList: { toggle(): void {} },
  } as unknown as HTMLElement;
  const bannerTitle = {
    textContent: "项目内容已在别处更新",
    dataset: {
      stale: "项目内容已在别处更新",
      uncertain: "拖放请求状态不明确",
    },
  } as unknown as HTMLElement;
  const bannerCopy = {
    textContent: "本页已转为只读，请重新打开初稿。当前已载入内容仍可查看。",
    dataset: {
      stale: "本页已转为只读，请重新打开初稿。当前已载入内容仍可查看。",
      uncertain: "本页已转为只读，请重新打开初稿后确认请求是否已生效。",
    },
  } as unknown as HTMLElement;
  const banner = {
    hidden: true,
    children: [bannerTitle, bannerCopy],
    querySelector: (selector: string) => selector === "strong" ? bannerTitle : bannerCopy,
  } as unknown as HTMLElement & { hidden: boolean };
  const elements: Record<string, HTMLElement> = {
    "draft-document": host,
    "draft-editor-shell": shell,
    "draft-confirm": { disabled: false } as unknown as HTMLElement,
    "draft-confirm-heading": { textContent: "" } as unknown as HTMLElement,
    "draft-confirm-copy": { textContent: "" } as unknown as HTMLElement,
    "draft-stale": banner,
  };
  vi.stubGlobal("document", {
    getElementById: (id: string) => elements[id] ?? null,
    elementsFromPoint: (x: number) => state.hit === "outside"
      ? [{ closest: () => null }]
      : [state.surface === "source"
        ? sourceFragment
        : state.surface === "draft" && x < 40
          ? draftSourceFragment
          : targetFragment],
    createRange: () => range,
    createElement: () => {
      state.indicatorCreates += 1;
      return {
        dataset: {},
        style: {},
        setAttribute(): void {},
        className: "",
        remove(): void {},
      };
    },
    querySelectorAll: () => [],
  });
  return Object.assign(state, { host, shell, banner, bannerTitle, bannerCopy });
}

function serverTiming(): DraftEditServerTiming {
  return {
    selection_caret_revalidation_ms: 1,
    immutable_child_write_fsync_ms: 2,
    project_brief_transcript_context_validation_ms: 3,
    workflow_snapshot_refresh_ms: 4,
    draft_snapshot_rebuild_ms: 5,
    server_before_response_ms: 6,
  };
}

function fragment(
  paragraphId: string,
  textNode: Text,
  start: number,
): HTMLElement {
  const fragment = {
    dataset: { textFragment: "true", utf16Start: String(start) },
    textContent: textNode.textContent,
    firstChild: textNode,
    closest: () => paragraphId === "source_a"
      ? sourceRow
      : paragraphId === "paragraph_a"
        ? draftSourceRow
        : targetRow,
  } as unknown as HTMLElement;
  void paragraphId;
  return fragment;
}

type MutableFragment = {
  childNodes: Node[];
  firstChild: Node | null;
  textContent: string | null;
  normalize: () => void;
};

function row(paragraphId: string, surface: "draft" | "source", fragment: HTMLElement): HTMLElement {
  return {
    dataset: { editorParagraph: paragraphId, surface },
    querySelectorAll: () => [fragment],
    querySelector: () => null,
    normalize: () => {},
    closest: () => null,
  } as unknown as HTMLElement;
}

let sourceRow!: HTMLElement;
let draftSourceRow!: HTMLElement;
let targetRow!: HTMLElement;

function dragSnapshot(): DraftEditorSnapshot {
  return {
    editor_schema_version: 1,
    review_mode: "draft_editor",
    project: { name: "测试项目" },
    brief: { theme: "测试", target_duration_ticks: 0, focus: [], allow_reorder: true },
    candidate: { candidate_id: "candidate_a", parent_candidate_id: null, display_title: null, confirmed_by_user: false, has_unrecorded_narration: false },
    paragraphs: [
      paragraph("paragraph_a", "甲😀乙宽窄", "block_a"),
      paragraph("paragraph_b", "目标文本宽窄", "block_b"),
    ],
    sources: [],
    history: { can_undo: true, can_redo: false, redo_scope: "draft_workspace" },
    workspace: basis("candidate_a"),
    transcript_browser: { read_endpoint: "/read", window_endpoint: "/window", search_endpoint: "/search", selection_endpoint: "/selection", pagination: { offset_unit: "paragraph", max_limit: 1 } },
    candidate_handoff: { endpoint: "/api/workflow/draft-candidate-select", requires_exact_parent: true },
  };
}

function nextDragSnapshot(
  previous: DraftEditorSnapshot,
  candidateId: string,
  generation: number,
  checkpointHashSeed: string,
  contentHashSeed: string,
): DraftEditorSnapshot {
  return {
    ...previous,
    candidate: {
      ...previous.candidate,
      candidate_id: candidateId,
      parent_candidate_id: previous.candidate.candidate_id,
    },
    workspace: {
      expected_checkpoint_ref: {
        generation,
        checkpoint_hash: checkpointHashSeed.repeat(64),
      },
      expected_current_candidate_ref: {
        artifact_id: candidateId,
        schema_version: 2,
        content_hash: contentHashSeed.repeat(64),
      },
    },
  };
}

function sectionHistorySnapshot(
  previous: DraftEditorSnapshot,
  candidateId: string,
  generation: number,
  headings: Array<[string, string]>,
  canUndo: boolean,
  canRedo: boolean,
): DraftEditorSnapshot {
  const next = generation === 1
    ? previous
    : nextDragSnapshot(previous, candidateId, generation, "c", "d");
  return {
    ...next,
    candidate: {
      ...next.candidate,
      candidate_id: candidateId,
    },
    blocks: headings.map(([block_id, title]) => ({
      block_id,
      kind: "section_title" as const,
      title,
    })),
    history: {
      can_undo: canUndo,
      can_redo: canRedo,
      redo_scope: "draft_workspace",
    },
  };
}

function shortcut(key: string, shiftKey = false): KeyboardEvent {
  return {
    key,
    target: null,
    metaKey: true,
    ctrlKey: false,
    shiftKey,
    preventDefault: vi.fn(),
  } as unknown as KeyboardEvent;
}

function paragraph(paragraphId: string, value: string, blockId: string): DraftEditorParagraph {
  return {
    paragraph_id: paragraphId,
    kind: "source_excerpt",
    person: { person_id: null, name: null, role: null, local_speaker_id: null },
    text: value,
    section_title: null,
    narration_status: null,
    block_id: blockId,
    source_runs: [{ block_id: blockId, source_id: "source_a", source_display_name: "素材", paragraph_id: paragraphId, start_ticks: 0, end_ticks: 1, start_offset: 0, end_offset: Array.from(value).length, source_start_offset: 0, source_end_offset: Array.from(value).length, text: value, refs: [ref()] }],
    exact_refs: [ref()],
  };
}

function targetSelection(candidateId = "candidate_a"): ActiveSelection {
  return {
    surface: "draft",
    request: { anchor: point("paragraph_a", 1), focus: point("paragraph_a", 3) },
    response: {
      candidate_id: candidateId,
      surface: "draft",
      resolution: { direction: "forward", canonical_text: "😀乙", refs: [{ ...ref(), canonical_text: "😀乙" }], start_caret: null, end_caret: null, adjusted: false, degraded: false, degradation_reasons: [] },
      display_range: { anchor: { paragraph_id: "paragraph_a", character_offset: 1, utf16_offset: 1 }, focus: { paragraph_id: "paragraph_a", character_offset: 3, utf16_offset: 3 } },
      correspondence_groups: [],
      resolution_hash: "a".repeat(64),
    },
    acceptedDegraded: true,
  };
}

function sourceSelection(): ActiveSelection {
  const selected = targetSelection();
  return {
    ...selected,
    surface: "source",
    request: { anchor: point("source_a", 0), focus: point("source_a", 3) },
    response: {
      ...selected.response,
      surface: "source",
      display_range: { anchor: { paragraph_id: "source_a", character_offset: 0, utf16_offset: 0 }, focus: { paragraph_id: "source_a", character_offset: 3, utf16_offset: 3 } },
      resolution: { ...selected.response.resolution, canonical_text: "甲😀", refs: [{ ...ref(), canonical_text: "甲😀" }] },
      resolution_hash: undefined,
    },
  };
}

function ref(): { source_id: string; transcript_version_id: string; segment_id: string; start_ticks: number; end_ticks: number } {
  return { source_id: "source_a", transcript_version_id: "transcript_a", segment_id: "segment_a", start_ticks: 0, end_ticks: 1 };
}

function point(paragraphId: string, offset: number): DraftEditorRequestPoint {
  return { paragraph_id: paragraphId, offset, offset_encoding: "utf16" };
}

function sectionTarget(row: HTMLElement, isHandle: boolean): HTMLElement {
  const target = {} as HTMLElement;
  target.closest = ((selector: string) => {
      if (selector === "[data-section-drag-handle]") return isHandle ? target : null;
      if (selector === "[data-section-row]") return row;
      return null;
    }) as typeof target.closest;
  return target;
}

function pointerEvent(target: HTMLElement): PointerEvent {
  return {
    target,
    currentTarget: null,
    clientX: 0,
    clientY: 0,
    pointerId: 1,
  } as unknown as PointerEvent;
}

function basis(candidateId: string): DraftWorkspaceMutationBasis {
  return { expected_checkpoint_ref: { generation: 1, checkpoint_hash: "a".repeat(64) }, expected_current_candidate_ref: { artifact_id: candidateId, schema_version: 2, content_hash: "b".repeat(64) } };
}
