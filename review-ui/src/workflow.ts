import { ticksToSeconds } from "./player";
import { copyDraftAgentHandoff } from "./agent-handoff";
import {
  DraftEditorViewState,
  caretOffsetForParagraph,
  codePointToUtf16,
  contiguousPatch,
  displayRangeForParagraph,
  optionalDisplayTitle,
  paragraphIdsForDisplayRange,
  partitionText,
  utf16ToCodePoint,
  type DraftEditorSurface,
  type TextDecoration,
  type TextRun,
} from "./draft-editor-model";
import { endpointFromDom } from "./draft-dom-endpoint";
import { DraftPointerGesture } from "./draft-pointer-gesture";
import { visibleSelectionQuote } from "./draft-visible-selection";
import {
  draftCorrespondences,
  type DraftCorrespondence,
} from "./draft-correspondence";
import {
  DraftSelectionGeneration,
  type DraftSelectionToken,
} from "./draft-selection-generation";
import { DraftPlaybackQueue } from "./draft-playback";
import {
  PuncSession,
  PUNCTUATION_NON_PUNCTUATION_MESSAGE,
  type PunctuationInputMode,
  type PunctuationSessionBinding,
} from "./draft-punctuation-editor";
import {
  DraftTranscriptWindowState,
  TRANSCRIPT_WINDOW_LIMIT,
  type AdjacentTranscriptWindowRequest,
  type TranscriptWindowRequest,
} from "./draft-transcript-window";
import {
  draftEditPerformanceDataset,
  DraftEditPerformanceTrace,
  serverDraftEditTimingDataset,
  type DraftEditOperation,
  type DraftEditServerTiming,
} from "./draft-edit-performance";
import { userFacingErrorMessage } from "./user-message";
import {
  performWorkflowProposalHandoff,
  type WorkflowProposalHandoff,
} from "./workflow-handoff";
import type {
  DraftEditorCaret,
  DraftEditorParagraph,
  DraftEditorRequestPoint,
  DraftEditorSearchMatch,
  DraftEditorSearchPage,
  DraftEditorSelectionRequest,
  DraftEditorSelectionResponse,
  DraftEditorSnapshot,
  DraftWorkspaceMutationBasis,
  DraftTranscriptWindow,
  ReadableParagraph,
  ResolvedSelectionRef,
  WorkflowApi,
  WorkflowReviewPayload,
} from "./workflow-types";

const SEARCH_PAGE_SIZE = 200;
const TICKS_PER_SECOND = 120_000;

const DRAFT_SURFACE = "draft" as const;
const SOURCE_SURFACE = "source" as const;
const DRAFT_EDITOR_SHELL_ID = "draft-editor-shell";
const DRAFT_DOCUMENT_ID = "draft-document";
const DRAFT_SOURCE_DOCUMENT_ID = "draft-source-document";
const DRAFT_SOURCE_SELECT_ID = "draft-source-select";
const DRAFT_SECTIONS_ID = "draft-sections";
const DRAFT_PLAYER_ID = "draft-player";
const DRAFT_PLAYER_CAPTION_ID = "draft-player-caption";
const DRAFT_UNDO_ID = "draft-undo";
const DRAFT_REDO_ID = "draft-redo";
const EDITOR_PARAGRAPH_SELECTOR = "[data-editor-paragraph]";
const SECTION_ROW_SELECTOR = "[data-section-row]";
const SECTION_TITLE_KIND = "section_title";
const SOURCE_EXCERPT_KIND = "source_excerpt";
const NARRATION_KIND = "narration";
const DRAFT_EDITOR_API = "/api/workflow/draft-editor";
const DRAFT_EDIT_API = "/api/workflow/draft-edit";
const ARIA_HIDDEN = "aria-hidden";
const ARIA_BUSY = "aria-busy";
const ARIA_LABEL = "aria-label";
const ARIA_SELECTED = "aria-selected";
const SECTIONS_MODE = "sections" as const;
const BUTTON_ELEMENT = "button";
const INPUT_ELEMENT = "input";
const FUNCTION_TYPE = "function";
const POINTERDOWN_EVENT = "pointerdown";
const POINTERMOVE_HIT_TEST_PHASE = "pointermove_hit_test";
const DROP_HTTP_SERVER_PHASE = "drop_http_server";
const DOM_PATCH_PHASE = "dom_patch";
const SELECTION_RESOLVE_PHASE = "selection_resolve";
const ARROW_LEFT = "ArrowLeft" as const;
const ARROW_RIGHT = "ArrowRight" as const;
const ARROW_UP = "ArrowUp" as const;
const ARROW_DOWN = "ArrowDown" as const;
const LOADED_METADATA_EVENT = "loadedmetadata";
const INVALID_WORKFLOW_CHANGE = "invalid_workflow_change";
const WARNING_CLASS = "warning";

interface SearchState {
  open: boolean;
  query: string;
  matches: DraftEditorSearchMatch[];
  current: number;
  timer: number | null;
}

type DraftLockReason = "stale" | "uncertain";

interface ResultSelectionWire {
  surface: "draft";
  request: DraftEditorSelectionRequest;
  response: DraftEditorSelectionResponse;
  accepted_degraded: boolean;
}

interface EditResponse {
  draft_editor: DraftEditorSnapshot;
  timing?: DraftEditServerTiming;
  result_selection?: ResultSelectionWire;
}

type DraftDropSource =
  | {
      kind: "resolved_selection";
      resolution_hash: string;
      surface: "draft";
      selection_kind: "source_excerpt";
      display_range: {
        anchor: { paragraph_id: string; block_id: string; utf16_offset: number };
        focus: { paragraph_id: string; block_id: string; utf16_offset: number };
      };
      block_ids: string[];
      refs: Omit<ResolvedSelectionRef, "canonical_text">[];
      canonical_text: string;
      degraded: boolean;
    }
  | {
      kind: "narration_block";
      surface: "draft";
      block_id: string;
      text: string;
      status: typeof DRAFT_SURFACE | "approved" | "recorded";
      recorded_refs: Omit<ResolvedSelectionRef, "canonical_text">[];
    }
  | {
      kind: "exact_source_refs";
      source_id: string;
      transcript_version_id: string;
      refs: Omit<ResolvedSelectionRef, "canonical_text">[];
      canonical_text: string;
    };

interface FrozenDraftDrop {
  generation: number;
  operation_id: string;
  basis: DraftWorkspaceMutationBasis;
  candidate_id: string;
  operation: "move_selection" | "insert_source_refs";
  source: DraftDropSource;
  accept_degraded: boolean;
  resolvedRange: DraftEditorSelectionResponse["display_range"] | null;
}

interface NarrationResponse {
  draft_editor: DraftEditorSnapshot;
}

interface SectionDragGeometry {
  headingId: string;
  row: HTMLElement;
  top: number;
  bottom: number;
  height: number;
}

type NarrationEditorState = "reading" | "warning" | "editing";

export type DraftWorkspaceMutationAction =
  | "draft-edit:delete"
  | "draft-edit:move"
  | "draft-edit:insert"
  | "draft-edit:punctuation_edit"
  | "draft-narration"
  | typeof DRAFT_UNDO_ID
  | typeof DRAFT_REDO_ID
  | "draft-edit:section_reorder"
  | "draft-edit:section_rename"
  | "draft-edit:section_split"
  | "draft-edit:section_merge"
  | "draft-edit:section_delete"
  | "approve-draft";

type DraftWorkspaceMutationEnvelope = DraftWorkspaceMutationBasis & {
  operation_id: string;
};

export class DraftWorkspaceMutationRetry {
  private current: {
    key: string;
    envelope: DraftWorkspaceMutationEnvelope;
  } | null = null;

  constructor(
    private readonly createOperationId: (generation: number) => string =
      draftWorkspaceOperationId,
  ) {}

  mutation<T extends object>(
    action: DraftWorkspaceMutationAction,
    input: T,
    basis: DraftWorkspaceMutationBasis,
  ): T & DraftWorkspaceMutationEnvelope {
    const key = JSON.stringify({ action, input, basis });
    if (this.current === null || this.current.key !== key) {
      this.current = {
        key,
        envelope: {
          operation_id: this.createOperationId(
            basis.expected_checkpoint_ref.generation,
          ),
          expected_checkpoint_ref: basis.expected_checkpoint_ref,
          expected_current_candidate_ref: basis.expected_current_candidate_ref,
        },
      };
    }
    return { ...input, ...this.current.envelope };
  }

  clear(): void {
    this.current = null;
  }
}

export function locksDraftWorkspaceWrites(error: unknown): boolean {
  const code = (error as { code?: unknown }).code;
  return (
    code === "stale_review"
    || code === "draft_workspace_stale"
    || code === "draft_workspace_action_conflict"
  );
}

export class WorkflowReviewController {
  private readonly api: WorkflowApi;
  private readonly onStatus: (text: string, warning?: boolean) => void;
  private readonly onProposalHandoff: (
    handoff: WorkflowProposalHandoff,
  ) => Promise<void>;
  private snapshot: DraftEditorSnapshot | null = null;
  private readonly view = new DraftEditorViewState("");
  private readonly transcriptWindow = new DraftTranscriptWindowState("");
  private busy = false;
  private selectionPending = false;
  private pendingDraftDrop: FrozenDraftDrop | null = null;
  private lockReason: DraftLockReason | null = null;
  private readonly selectionGeneration = new DraftSelectionGeneration();
  private readonly caretGeneration = new DraftSelectionGeneration();
  private readonly pointerGesture = new DraftPointerGesture();
  private readonly editPerformanceTrace = new DraftEditPerformanceTrace();
  private selectionTimingStartedAt: number | null = null;
  private transcriptRequestGeneration = 0;
  private transcriptScrollFrame: number | null = null;
  private transcriptScrollLocked = false;
  private sourceSelectionDragging = false;
  private transcriptExtensionPending = false;
  private readonly playback = new DraftPlaybackQueue();
  private playerExpanded = false;
  private proposalRetryRequired = false;
  private readonly retryableWorkspaceMutation = new DraftWorkspaceMutationRetry();
  private degradedPromptOperation: "delete" | "move" | "insert" | null = null;
  private readonly narrationEditorStates = new Map<string, NarrationEditorState>();
  private punc: PuncSession | null = null;
  private pInput: HTMLInputElement | null = null;
  private pStart = 0;
  private pEnd = 0;
  private pComposition = "";
  private pFlush: Promise<boolean> | null = null;
  private pPreview: { paragraphId: string; text: string } | null = null;
  private draftDrag:
    | {
        surface: DraftEditorSurface;
        target: DraftEditorRequestPoint | null;
        blockId: string | null;
        narrationBlockId: string | null;
        dragging: boolean;
        pointerTarget: HTMLElement | null;
        pointerId: number | null;
        generation: number;
        frozen: FrozenDraftDrop | null;
      }
    | null = null;
  private draftDragGeneration = 0;
  private sectionDrag:
    | {
        headingId: string;
        originX: number;
        originY: number;
        targetHeadingId: string | null;
        validTarget: boolean;
        dragging: boolean;
        pointerTarget: HTMLElement | null;
        pointerId: number | null;
        layout: SectionDragGeometry[];
      }
    | null = null;
  private draftDropIndicator: HTMLElement | null = null;
  private draftDropIndicatorRow: HTMLElement | null = null;
  private draftDropIndicatorKey: string | null = null;
  private sectionDropIndicator: HTMLElement | null = null;
  private sectionDropIndicatorKey: string | null = null;
  private sectionDraggingRow: HTMLElement | null = null;
  private suppressSectionClick = false;
  private correspondences: DraftCorrespondence[] = [];
  private correspondenceIndex = 0;
  private search: Record<DraftEditorSurface, SearchState> = {
    draft: { open: false, query: "", matches: [], current: 0, timer: null },
    source: { open: false, query: "", matches: [], current: 0, timer: null },
  };

  constructor(
    initial: DraftEditorSnapshot | WorkflowReviewPayload,
    api: WorkflowApi,
    onStatus: (text: string, warning?: boolean) => void,
    onProposalHandoff: (handoff: WorkflowProposalHandoff) => Promise<void>,
  ) {
    this.api = api;
    this.onStatus = onStatus;
    this.onProposalHandoff = onProposalHandoff;
    if (initial.review_mode === "draft_editor") {
      this.snapshot = initial;
    }
  }

  async mount(): Promise<void> {
    document.body.classList.add("draft-editor-mode");
    element<HTMLElement>("workflow-review").hidden = false;
    this.bindEvents();
    this.snapshot ??= await this.api<DraftEditorSnapshot>(
      DRAFT_EDITOR_API,
    );
    this.view.currentSourceId = this.snapshot.sources[0]?.source_id ?? "";
    this.transcriptWindow.reset(this.view.currentSourceId);
    await this.loadTranscriptWindow({
      sourceId: this.view.currentSourceId,
      offset: 0,
    });
    this.renderInitial();
    this.onStatus("初稿已载入。选择文字进行删除、移动、试听或从原稿加入。");
  }

  private bindEvents(): void {
    document.addEventListener(POINTERDOWN_EVENT, (event) => {
      const targetMenu = event.target instanceof Element
        ? event.target.closest("[data-section-menu]")
        : null;
      document.querySelectorAll<HTMLDetailsElement>("[data-section-menu][open]").forEach((menu) => {
        if (menu !== targetMenu) menu.open = false;
      });
    });
    element<HTMLElement>(DRAFT_DOCUMENT_ID).addEventListener(
      "pointerup",
      () => void this.handleDocumentPointerUp(DRAFT_SURFACE),
    );
    element<HTMLElement>(DRAFT_DOCUMENT_ID).addEventListener(
      POINTERDOWN_EVENT,
      (event) => this.handleDragPointerDown(DRAFT_SURFACE, event),
    );
    element<HTMLElement>(DRAFT_DOCUMENT_ID).addEventListener("click", (event) => {
      const button = (event.target as HTMLElement | null)?.closest<HTMLButtonElement>(
        "[data-narration-action]",
      );
      if (button !== null && button !== undefined) this.handleNarrationAction(button);
    });
    element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID).addEventListener(
      "pointerup",
      () => {
        this.sourceSelectionDragging = false;
        void this.handleDocumentPointerUp(SOURCE_SURFACE);
      },
    );
    element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID).addEventListener(
      POINTERDOWN_EVENT,
      (event) => {
        this.sourceSelectionDragging = true;
        this.handleDragPointerDown(SOURCE_SURFACE, event);
      },
    );
    window.addEventListener("pointermove", (event) => this.handleDragPointerMove(event));
    element<HTMLElement>(DRAFT_SECTIONS_ID).addEventListener(
      POINTERDOWN_EVENT,
      (event) => this.handleSectionPointerDown(event),
    );
    element<HTMLElement>(DRAFT_SECTIONS_ID).addEventListener(
      "click",
      (event) => this.handleSectionRowClick(event),
    );
    window.addEventListener("pointermove", (event) => this.handleSectionPointerMove(event));
    window.addEventListener("pointerup", (event) => {
      this.sourceSelectionDragging = false;
      void this.handleSectionPointerUp(event);
      void this.handleDragPointerUp(event);
    });
    window.addEventListener("pointercancel", (event) => {
      this.sourceSelectionDragging = false;
      this.handleSectionPointerCancel(event);
      this.handleDragPointerCancel(event);
    });
    window.addEventListener("beforeunload", (event) => {
      if (this.punc?.changed) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        this.cancelDrag();
        this.cancelSectionDrag();
      }
    });
    for (const surface of [DRAFT_SURFACE, SOURCE_SURFACE] as const) {
      const documentElement = element<HTMLElement>(
        surface === DRAFT_SURFACE ? DRAFT_DOCUMENT_ID : DRAFT_SOURCE_DOCUMENT_ID,
      );
      documentElement.addEventListener("keyup", (event) => {
        if (event.shiftKey) void this.captureSelection(surface);
      });
      documentElement.addEventListener("click", (event) => {
        void this.handleDocumentClick(surface, event);
      });
    }

    element<HTMLSelectElement>(DRAFT_SOURCE_SELECT_ID).addEventListener("change", (event) => {
      void this.selectSource((event.currentTarget as HTMLSelectElement).value);
    });
    element<HTMLButtonElement>("draft-delete").addEventListener(
      "click",
      () => void this.applyEdit("delete"),
    );
    element<HTMLButtonElement>("draft-section-split").addEventListener(
      "click",
      () => void this.startSectionSplitFromCaret(),
    );
    element<HTMLButtonElement>("draft-mode-body").addEventListener(
      "click",
      () => void this.setDraftMode("body"),
    );
    element<HTMLButtonElement>("draft-mode-sections").addEventListener(
      "click",
      () => void this.setDraftMode(SECTIONS_MODE),
    );
    element<HTMLButtonElement>(DRAFT_UNDO_ID).addEventListener(
      "click",
      () => void this.navigateHistory(false),
    );
    element<HTMLButtonElement>(DRAFT_REDO_ID).addEventListener(
      "click",
      () => void this.navigateHistory(true),
    );
    element<HTMLButtonElement>("draft-confirm").addEventListener(
      "click",
      () => void this.confirmDraft(),
    );
    element<HTMLButtonElement>("draft-agent-copy").addEventListener(
      "click",
      () => void this.copyAgentHandoff(),
    );
    element<HTMLButtonElement>("draft-degraded-cancel").addEventListener("click", () => {
      const surface = this.view.lastSelectionSurface;
      const affected = this.selectionParagraphIds(surface);
      this.degradedPromptOperation = null;
      this.view.setSelection(surface, null);
      this.patchParagraphs(surface, affected);
      this.renderInteractionState();
    });
    element<HTMLButtonElement>("draft-degraded-accept").addEventListener("click", () => {
      const selection = this.view.selectionFor(this.view.lastSelectionSurface);
      if (selection !== null) {
        selection.acceptedDegraded = true;
        this.degradedPromptOperation = null;
        this.renderInteractionState();
      }
    });
    element<HTMLButtonElement>("draft-player-toggle").addEventListener(
      "click",
      () => this.togglePlayer(),
    );
    element<HTMLButtonElement>("draft-correspondence-prev").addEventListener(
      "click",
      () => void this.stepCorrespondence(-1),
    );
    element<HTMLButtonElement>("draft-correspondence-next").addEventListener(
      "click",
      () => void this.stepCorrespondence(1),
    );

    document.querySelectorAll<HTMLButtonElement>("[data-draft-pane]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!await this.flushPunc()) return;
        const pane = button.dataset.draftPane;
        if (pane === DRAFT_SURFACE || pane === SOURCE_SURFACE || pane === "player") {
          this.view.selectPane(pane);
          this.renderPaneState();
        }
      });
    });
    document.querySelectorAll<HTMLButtonElement>("[data-draft-find]").forEach((button) => {
      button.addEventListener("click", () => {
        const surface = button.dataset.draftFind;
        if (surface === DRAFT_SURFACE || surface === SOURCE_SURFACE) this.openSearch(surface);
      });
    });
    document.querySelectorAll<HTMLElement>("[data-draft-find-panel]").forEach((panel) => {
      const surface = panel.dataset.draftFindPanel;
      if (surface !== DRAFT_SURFACE && surface !== SOURCE_SURFACE) return;
      panel.querySelector<HTMLInputElement>(INPUT_ELEMENT)?.addEventListener(INPUT_ELEMENT, (event) => {
        this.scheduleSearch(surface, (event.currentTarget as HTMLInputElement).value);
      });
      panel.querySelector<HTMLButtonElement>("[data-draft-find-prev]")?.addEventListener(
        "click",
        () => void this.stepSearch(surface, -1),
      );
      panel.querySelector<HTMLButtonElement>("[data-draft-find-next]")?.addEventListener(
        "click",
        () => void this.stepSearch(surface, 1),
      );
      panel.querySelector<HTMLButtonElement>("[data-draft-find-close]")?.addEventListener(
        "click",
        () => this.closeSearch(surface),
      );
    });

    const player = element<HTMLVideoElement>(DRAFT_PLAYER_ID);
    player.addEventListener("timeupdate", () => {
      const current = this.playback.current;
      if (
        current !== null
        && player.currentTime >= ticksToSeconds(current.endTicks)
      ) {
        void this.advancePlayback();
      }
    });
    player.addEventListener("ended", () => void this.advancePlayback());
    player.addEventListener("error", () => {
      const message = element<HTMLElement>("draft-media-error");
      message.hidden = false;
      message.textContent = "当前素材无法在浏览器中播放；文稿编辑仍可继续。";
    });
    element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID).addEventListener("scroll", () => {
      if (this.transcriptScrollLocked || this.transcriptScrollFrame !== null) return;
      this.transcriptScrollFrame = window.requestAnimationFrame(() => {
        this.transcriptScrollFrame = null;
        const host = element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID);
        if (this.sourceSelectionDragging) {
          const adjacent = this.transcriptWindow.requestForSelectionScroll(
            host.scrollTop,
          );
          if (adjacent !== null && !this.transcriptExtensionPending) {
            void this.loadAdjacentTranscriptWindow(adjacent);
          }
          return;
        }
        const request = this.transcriptWindow.requestForScroll(host.scrollTop);
        if (request !== null) void this.loadTranscriptWindow(request);
      });
    }, { passive: true });
    window.addEventListener("keydown", (event) => void this.handleShortcut(event));
  }

  private async copyAgentHandoff(): Promise<void> {
    const snapshot = this.requireSnapshot();
    const selection = this.view.selectionFor(this.view.lastSelectionSurface);
    const clipboard = navigator.clipboard?.writeText === undefined
      ? null
      : navigator.clipboard;
    const result = await copyDraftAgentHandoff(
      this.api,
      clipboard,
      snapshot.candidate.candidate_id,
      selection === null
        ? null
        : visibleSelectionQuote(
          selection.surface === DRAFT_SURFACE
            ? snapshot.paragraphs
            : this.transcriptWindow.paragraphs,
          selection.request,
        ),
    );
    this.renderAgentHandoffStatus(result.message, !result.ok);
  }

  private renderAgentHandoffStatus(message: string, warning: boolean): void {
    const status = element<HTMLElement>("draft-agent-copy-status");
    status.hidden = false;
    status.textContent = message;
    status.classList.toggle(WARNING_CLASS, warning);
  }

  private renderInitial(): void {
    const snapshot = this.requireSnapshot();
    element<HTMLElement>("project-name").textContent = snapshot.project.name;
    element<HTMLElement>("revision").textContent = "";
    const title = element<HTMLElement>("draft-theme");
    title.textContent = optionalDisplayTitle(snapshot.candidate.display_title) ?? "";
    title.hidden = title.textContent.length === 0;
    element<HTMLElement>("draft-theme-kicker").textContent = "初稿调整";
    this.renderSourceSelect();
    this.renderDraft();
    this.renderSource();
    this.renderSearch(DRAFT_SURFACE);
    this.renderSearch(SOURCE_SURFACE);
    this.renderPaneState();
    this.renderPlayer();
    this.renderCorrespondenceNavigation();
    this.renderToolbar();
    this.renderDegradedPrompt();
    this.renderConfirm();
  }

  private renderInteractionState(): void {
    this.renderToolbar();
    this.renderDegradedPrompt();
    this.renderConfirm();
    this.renderDraftControlsDisabled();
  }

  private renderSourceSelect(): void {
    const select = element<HTMLSelectElement>(DRAFT_SOURCE_SELECT_ID);
    const snapshot = this.requireSnapshot();
    select.replaceChildren(
      ...snapshot.sources.map((source) => {
        const option = document.createElement("option");
        option.value = source.source_id;
        option.textContent = source.display_name;
        return option;
      }),
    );
    if (!snapshot.sources.some((source) => source.source_id === this.view.currentSourceId)) {
      this.view.currentSourceId = snapshot.sources[0]?.source_id ?? "";
    }
    select.value = this.view.currentSourceId;
  }

  private renderDraft(): void {
    const host = element<HTMLElement>(DRAFT_DOCUMENT_ID);
    const sections = element<HTMLElement>(DRAFT_SECTIONS_ID);
    host.hidden = this.view.draftMode !== "body";
    sections.hidden = this.view.draftMode !== SECTIONS_MODE;
    if (this.view.draftMode === SECTIONS_MODE) {
      this.renderSections();
      return;
    }
    host.replaceChildren(
      ...this.requireSnapshot().paragraphs.map((paragraph) => (
        paragraph.kind === NARRATION_KIND
          ? this.renderNarration(paragraph)
          : this.renderTextParagraph(DRAFT_SURFACE, paragraph)
      )),
    );
  }

  private async setDraftMode(mode: "body" | "sections"): Promise<void> {
    if (!await this.flushPunc() || this.busy) return;
    this.cancelDrag();
    this.view.setDraftMode(mode);
    this.renderDraft();
    const body = element<HTMLButtonElement>("draft-mode-body");
    const sections = element<HTMLButtonElement>("draft-mode-sections");
    body.classList.toggle("is-active", mode === "body");
    sections.classList.toggle("is-active", mode === SECTIONS_MODE);
    body.setAttribute(ARIA_SELECTED, String(mode === "body"));
    sections.setAttribute(ARIA_SELECTED, String(mode === SECTIONS_MODE));
    this.renderInteractionState();
  }

  private renderSections(): void {
    const host = element<HTMLElement>(DRAFT_SECTIONS_ID);
    const snapshot = this.requireSnapshot();
    const blocks = snapshot.blocks ?? [];
    const headings = blocks
      .map((block, index) => ({ block, index }))
      .filter((item): item is { block: import("./workflow-types").SectionTitleBlock; index: number } => item.block.kind === SECTION_TITLE_KIND);
    host.replaceChildren(
      ...headings.map(({ block, index }, headingIndex) => {
        const row = document.createElement("section");
        row.className = "draft-section-row";
        row.dataset.headingBlockId = block.block_id;
        row.dataset.sectionRow = "true";
        const title = document.createElement("h3");
        title.textContent = block.title;
        const count = document.createElement("span");
        const next = headings[headingIndex + 1]?.index ?? blocks.length;
        const itemCount = Math.max(0, next - index - 1);
        count.textContent = itemCount === 0 ? "空章" : `${itemCount} 项`;
        const handle = document.createElement("span");
        handle.className = "draft-section-drag-handle";
        handle.dataset.sectionDragHandle = "true";
        handle.setAttribute("role", BUTTON_ELEMENT);
        handle.setAttribute(ARIA_LABEL, `拖动章节：${block.title}`);
        handle.textContent = "⋮⋮";
        const menu = document.createElement("details");
        menu.className = "draft-section-menu";
        menu.dataset.sectionMenu = "true";
        const summary = document.createElement("summary");
        summary.textContent = "…";
        summary.setAttribute(ARIA_LABEL, `章节操作：${block.title}`);
        menu.append(summary);
        const menuItems = document.createElement("div");
        menuItems.className = "draft-section-menu-items";
        const addMenuItem = (label: string, action: string): void => {
          const button = document.createElement(BUTTON_ELEMENT);
          button.type = BUTTON_ELEMENT;
          button.textContent = label;
          button.dataset.sectionAction = action;
          button.addEventListener("click", (event) => {
            event.stopPropagation();
            menu.open = false;
            void this.applySectionAction(action, block.block_id, headingIndex, headings);
          });
          menuItems.append(button);
        };
        addMenuItem("重命名", "rename");
        if (headingIndex > 0) addMenuItem("并入上一章", "merge_previous");
        addMenuItem("删除本章及内容", "delete");
        menu.append(menuItems);
        row.append(handle, title, count, menu);
        return row;
      }),
    );
  }

  private sectionHeadingForCaret(): string | null {
    const caret = this.view.caret;
    if (caret === null) return null;
    const snapshot = this.requireSnapshot();
    const paragraph = snapshot.paragraphs.find(
      (candidate) => candidate.paragraph_id === caret.paragraph_id,
    );
    if (
      caret.candidate_id !== snapshot.candidate.candidate_id
      || paragraph === undefined
      || paragraph.kind === NARRATION_KIND
      || paragraph.kind === SECTION_TITLE_KIND
    ) return null;
    let blockId: string;
    try {
      blockId = this.schema2DisplayPoint({
        paragraph_id: caret.paragraph_id,
        character_offset: caret.character_offset,
        utf16_offset: caret.utf16_offset,
      }).block_id;
    } catch {
      return null;
    }
    const blocks = snapshot.blocks ?? [];
    const blockIndex = blocks.findIndex((block) => block.block_id === blockId);
    if (blockIndex < 0) return null;
    for (let index = blockIndex; index >= 0; index -= 1) {
      const block = blocks[index]!;
      if (block.kind === SECTION_TITLE_KIND) return block.block_id;
    }
    return null;
  }

  private async startSectionSplitFromCaret(): Promise<void> {
    if (
      this.view.draftMode !== "body"
      || this.view.locked
      || !await this.flushPunc()
      || this.busy
    ) return;
    const caret = this.view.caret;
    const headingId = this.sectionHeadingForCaret();
    if (caret === null || headingId === null) {
      this.onStatus("请先在有归属章节的正文中放置拆分位置；解说内部不能拆分章节。", true);
      return;
    }
    let target: { paragraph_id: string; block_id: string; utf16_offset: number };
    try {
      target = this.schema2DisplayPoint({
        paragraph_id: caret.paragraph_id,
        character_offset: caret.character_offset,
        utf16_offset: caret.utf16_offset,
      });
    } catch {
      this.onStatus("当前拆分位置无效，请重新放置正文光标。", true);
      return;
    }
    const title = window.prompt("新章节标题", "新章节")?.trim();
    if (title === undefined || title.length === 0) return;
    await this.applySectionOperation("section_split", {
      heading_block_id: headingId,
      target,
      title,
    });
  }

  private async applySectionAction(
    action: string,
    headingId: string,
    index: number,
    headings: { block: { block_id: string; title: string }; index: number }[],
  ): Promise<void> {
    if (this.view.locked || this.busy) return;
    const adjacent = headings[index - 1]?.block.block_id;
    let operation: Extract<DraftEditOperation, `section_${string}`>;
    let payload: Record<string, unknown>;
    if (action === "rename") {
      const title = window.prompt("章节标题", headings[index]!.block.title)?.trim();
      if (title === undefined || title.length === 0) return;
      operation = "section_rename";
      payload = { heading_block_id: headingId, title };
    } else if (action === "merge_previous") {
      if (adjacent === undefined) return;
      operation = "section_merge";
      payload = {
        heading_block_id: headingId,
        direction: "previous",
        adjacent_heading_block_id: adjacent,
      };
    } else if (action === "delete") {
      operation = "section_delete";
      payload = { heading_block_id: headingId };
    } else return;
    await this.applySectionOperation(operation, payload);
  }

  private handleSectionPointerDown(event: PointerEvent): void {
    if (this.view.draftMode !== SECTIONS_MODE || this.view.locked || this.busy) return;
    if (this.punc?.changed) {
      event.preventDefault();
      void this.flushPunc();
      return;
    }
    if (this.punc !== null) this.closePuncSession(this.punc);
    const target = event.target as HTMLElement | null;
    if (target?.closest("button, summary, [data-section-menu]") !== null) return;
    const handle = target?.closest<HTMLElement>("[data-section-drag-handle]");
    if (handle === null || handle === undefined) return;
    const row = handle.closest<HTMLElement>(SECTION_ROW_SELECTOR);
    const headingId = row?.dataset.headingBlockId;
    if (headingId === undefined) return;
    this.editPerformanceTrace.reset();
    this.editPerformanceTrace.record(SELECTION_RESOLVE_PHASE, 0);
    this.sectionDrag = {
      headingId,
      originX: event.clientX,
      originY: event.clientY,
      targetHeadingId: null,
      validTarget: false,
      dragging: false,
      pointerTarget: handle,
      pointerId: Number.isFinite(event.pointerId) ? event.pointerId : null,
      layout: [],
    };
  }

  private handleSectionPointerMove(event: PointerEvent): void {
    const drag = this.sectionDrag;
    if (drag === null) return;
    if (!drag.dragging && Math.hypot(event.clientX - drag.originX, event.clientY - drag.originY) > 4) {
      drag.dragging = true;
      drag.layout = this.captureSectionDragLayout();
      event.preventDefault();
      if (typeof window !== "undefined") window.getSelection()?.removeAllRanges();
      this.capturePointer(drag.pointerTarget, drag.pointerId);
      this.setBusinessDragState(true);
    }
    if (!drag.dragging) return;
    event.preventDefault();
    const hitTestStartedAt = performance.now();
    const rows = drag.layout;
    if (rows.length === 0) {
      drag.validTarget = false;
      drag.targetHeadingId = null;
      this.clearSectionDropIndicator();
      this.editPerformanceTrace.record(
        POINTERMOVE_HIT_TEST_PHASE,
        performance.now() - hitTestStartedAt,
      );
      return;
    }
    const sourceRow = rows.find((candidate) => candidate.headingId === drag.headingId)?.row;
    if (sourceRow !== undefined && this.sectionDraggingRow === null) {
      sourceRow.setAttribute("data-section-dragging", "true");
      this.sectionDraggingRow = sourceRow;
    }
    const first = rows[0]!;
    const last = rows[rows.length - 1]!;
    let next: SectionDragGeometry | undefined;
    if (event.clientY < first.top) {
      next = first;
    } else if (event.clientY > last.bottom) {
      next = undefined;
    } else {
      const row = rows.find(
        (candidate) => event.clientY >= candidate.top && event.clientY <= candidate.bottom,
      );
      if (row === undefined) {
        drag.validTarget = false;
        drag.targetHeadingId = null;
        this.clearSectionDropIndicator();
        this.editPerformanceTrace.record(
          POINTERMOVE_HIT_TEST_PHASE,
          performance.now() - hitTestStartedAt,
        );
        return;
      }
      const rowIndex = rows.indexOf(row);
      next = event.clientY < row.top + row.height / 2
        ? row
        : rows[rowIndex + 1];
    }
    drag.targetHeadingId = next?.headingId ?? null;
    const sourceIndex = rows.findIndex((candidate) => candidate.headingId === drag.headingId);
    const targetIndex = drag.targetHeadingId === null
      ? rows.length
      : rows.findIndex((candidate) => candidate.headingId === drag.targetHeadingId);
    drag.validTarget = drag.targetHeadingId !== drag.headingId
      && sourceIndex >= 0
      && targetIndex >= 0
      && targetIndex !== sourceIndex
      && targetIndex !== sourceIndex + 1;
    this.renderSectionDropIndicator(drag.validTarget ? drag.targetHeadingId : null, drag.validTarget);
    this.editPerformanceTrace.record(
      POINTERMOVE_HIT_TEST_PHASE,
      performance.now() - hitTestStartedAt,
    );
  }

  private async handleSectionPointerUp(event: PointerEvent): Promise<void> {
    const drag = this.sectionDrag;
    this.sectionDrag = null;
    if (drag?.dragging) this.suppressSectionClick = true;
    this.releasePointer(drag?.pointerTarget, drag?.pointerId);
    if (drag?.dragging) this.setBusinessDragState(false);
    this.clearSectionDropIndicator();
    if (drag === null || !drag.dragging || !drag.validTarget) return;
    event.preventDefault();
    await this.applySectionOperation("section_reorder", {
      heading_block_id: drag.headingId,
      before_heading_block_id: drag.targetHeadingId,
    });
  }

  private handleSectionPointerCancel(_event: PointerEvent): void {
    const drag = this.sectionDrag;
    this.sectionDrag = null;
    this.releasePointer(drag?.pointerTarget, drag?.pointerId);
    if (drag?.dragging) this.setBusinessDragState(false);
    this.clearSectionDropIndicator();
  }

  private handleSectionRowClick(event: MouseEvent): void {
    if (this.suppressSectionClick) {
      this.suppressSectionClick = false;
      return;
    }
    const target = event.target as HTMLElement | null;
    if (target?.closest("button, summary, [data-section-menu], [data-section-drag-handle]") !== null) return;
    const row = target?.closest<HTMLElement>(SECTION_ROW_SELECTOR);
    const headingId = row?.dataset.headingBlockId;
    if (headingId === undefined || this.view.draftMode !== SECTIONS_MODE) return;
    void this.setDraftMode("body");
    this.scrollToSectionHeading(headingId);
  }

  private captureSectionDragLayout(): SectionDragGeometry[] {
    return [...element<HTMLElement>(DRAFT_SECTIONS_ID).querySelectorAll<HTMLElement>(SECTION_ROW_SELECTOR)]
      .map((row) => {
        const rect = row.getBoundingClientRect();
        return {
          headingId: row.dataset.headingBlockId ?? "",
          row,
          top: rect.top,
          bottom: rect.bottom,
          height: rect.height,
        };
      })
      .filter((item) => item.headingId.length > 0);
  }

  private renderSectionDropIndicator(beforeHeadingId: string | null, valid: boolean): void {
    if (!valid) {
      this.clearSectionDropIndicator();
      return;
    }
    const key = beforeHeadingId ?? "__draft_section_end__";
    if (this.sectionDropIndicator !== null && this.sectionDropIndicatorKey === key) return;
    this.clearSectionDropIndicator();
    const indicator = document.createElement("div");
    indicator.className = "draft-section-drop-indicator";
    indicator.dataset.sectionDropIndicator = "true";
    indicator.setAttribute(ARIA_HIDDEN, "true");
    const host = element<HTMLElement>(DRAFT_SECTIONS_ID);
    const layout = this.sectionDrag?.layout ?? [];
    const row = beforeHeadingId === null
      ? null
      : layout.find((candidate) => candidate.headingId === beforeHeadingId) ?? null;
    const last = layout[layout.length - 1];
    const hostRect = host.getBoundingClientRect();
    const lineY = beforeHeadingId === null
      ? (last?.bottom ?? hostRect.top + hostRect.height) + 5
      : (row?.top ?? hostRect.top) - 2;
    indicator.style.top = `${lineY - hostRect.top + host.scrollTop - 2}px`;
    host.append(indicator);
    this.sectionDropIndicator = indicator;
    this.sectionDropIndicatorKey = key;
  }

  private clearSectionDropIndicator(): void {
    this.sectionDropIndicator?.remove();
    this.sectionDropIndicator = null;
    this.sectionDropIndicatorKey = null;
    if (this.sectionDraggingRow !== null) {
      delete this.sectionDraggingRow.dataset.sectionDragging;
      this.sectionDraggingRow = null;
    }
  }

  private cancelSectionDrag(): void {
    const drag = this.sectionDrag;
    this.sectionDrag = null;
    this.releasePointer(drag?.pointerTarget, drag?.pointerId);
    if (drag?.dragging) this.setBusinessDragState(false);
    this.clearSectionDropIndicator();
  }

  private async applySectionOperation(
    operation: Extract<DraftEditOperation, `section_${string}`>,
    payload: Record<string, unknown>,
  ): Promise<void> {
    if (!await this.flushPunc() || this.busy) return;
    if (operation !== "section_reorder") {
      this.editPerformanceTrace.reset();
      this.resetClientEditPerformanceDataset();
    }
    this.busy = true;
    this.renderInteractionState();
    try {
      const requestStartedAt = performance.now();
      const response = await this.api<EditResponse>(
        DRAFT_EDIT_API,
        post(this.workspaceMutation(`draft-edit:${operation}` as DraftWorkspaceMutationAction, {
          schema_version: 2,
          operation,
          payload,
        })),
      );
      this.editPerformanceTrace.record(
        DROP_HTTP_SERVER_PHASE,
        performance.now() - requestStartedAt,
      );
      if (response.timing !== undefined) {
        Object.assign(
          element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset,
          serverDraftEditTimingDataset(operation, response.timing),
        );
      }
      const domPatchStartedAt = performance.now();
      this.applySnapshot(response.draft_editor);
      this.renderSections();
      this.editPerformanceTrace.record(
        DOM_PATCH_PHASE,
        performance.now() - domPatchStartedAt,
      );
      this.publishEditPerformance(operation);
      this.onStatus("章节调整已保存为新的初稿版本。");
    } catch (error) {
      this.handleError(error);
    } finally {
      this.busy = false;
      this.renderInteractionState();
    }
  }

  private async selectSource(sourceId: string): Promise<void> {
    if (!await this.flushPunc()) return;
    if (sourceId === this.view.currentSourceId && this.transcriptWindow.paragraphs.length > 0) {
      return;
    }
    this.view.selectSource(sourceId);
    this.correspondences = [];
    this.correspondenceIndex = 0;
    this.transcriptWindow.reset(sourceId);
    element<HTMLSelectElement>(DRAFT_SOURCE_SELECT_ID).value = sourceId;
    await this.loadTranscriptWindow({ sourceId, offset: 0 });
    this.renderCorrespondenceNavigation();
  }

  private async loadTranscriptWindow(
    request: TranscriptWindowRequest,
    reveal = false,
  ): Promise<void> {
    const generation = ++this.transcriptRequestGeneration;
    const host = element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID);
    host.setAttribute(ARIA_BUSY, "true");
    try {
      const transcriptWindow = await this.api<DraftTranscriptWindow>(
        this.requireSnapshot().transcript_browser.window_endpoint,
        post({
          candidate_id: this.requireSnapshot().candidate.candidate_id,
          source_id: request.sourceId,
          offset: request.offset,
          limit: TRANSCRIPT_WINDOW_LIMIT,
          ...(request.paragraphId === undefined
            ? {}
            : { paragraph_id: request.paragraphId }),
        }),
      );
      if (generation !== this.transcriptRequestGeneration) return;
      if (request.sourceId !== this.view.currentSourceId) return;
      this.transcriptWindow.apply(transcriptWindow);
      if (reveal) this.transcriptScrollLocked = true;
      this.renderSource();
      if (reveal && request.paragraphId !== undefined) {
        host.scrollTop = this.transcriptWindow.topSpacerHeight();
        window.requestAnimationFrame(() => {
          this.scrollMountedParagraphNow(SOURCE_SURFACE, request.paragraphId!);
          window.requestAnimationFrame(() => {
            this.transcriptScrollLocked = false;
          });
        });
      }
    } catch (error) {
      if (generation === this.transcriptRequestGeneration) this.handleError(error);
    } finally {
      if (generation === this.transcriptRequestGeneration) {
        host.removeAttribute(ARIA_BUSY);
      }
    }
  }

  private async loadAdjacentTranscriptWindow(
    adjacent: AdjacentTranscriptWindowRequest,
  ): Promise<void> {
    this.transcriptExtensionPending = true;
    const host = element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID);
    host.setAttribute(ARIA_BUSY, "true");
    try {
      const transcriptWindow = await this.api<DraftTranscriptWindow>(
        this.requireSnapshot().transcript_browser.window_endpoint,
        post({
          candidate_id: this.requireSnapshot().candidate.candidate_id,
          source_id: adjacent.request.sourceId,
          offset: adjacent.request.offset,
          limit: TRANSCRIPT_WINDOW_LIMIT,
        }),
      );
      if (adjacent.request.sourceId !== this.view.currentSourceId) return;
      this.transcriptWindow.extend(transcriptWindow, adjacent.direction);
      this.extendSourceDocument(
        transcriptWindow.paragraphs,
        adjacent.direction,
      );
    } catch (error) {
      this.handleError(error);
    } finally {
      this.transcriptExtensionPending = false;
      host.removeAttribute(ARIA_BUSY);
    }
  }

  private renderSource(): void {
    const host = element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID);
    const source = this.requireSnapshot().sources.find(
      (item) => item.source_id === this.view.currentSourceId,
    );
    const heading = document.createElement("header");
    heading.className = "draft-source-reader-heading";
    const name = document.createElement("strong");
    name.textContent = source?.display_name ?? "原始转录稿";
    const detail = document.createElement("span");
    detail.textContent = source === undefined
      ? ""
      : `${formatDuration(source.duration_ticks)} · ${this.transcriptWindow.total} 段`;
    heading.append(name, detail);
    const topSpacer = document.createElement("div");
    topSpacer.className = "draft-transcript-spacer";
    topSpacer.dataset.transcriptSpacer = "top";
    topSpacer.style.height = `${this.transcriptWindow.topSpacerHeight()}px`;
    topSpacer.setAttribute(ARIA_HIDDEN, "true");
    const bottomSpacer = document.createElement("div");
    bottomSpacer.className = "draft-transcript-spacer";
    bottomSpacer.dataset.transcriptSpacer = "bottom";
    bottomSpacer.style.height = `${this.transcriptWindow.bottomSpacerHeight()}px`;
    bottomSpacer.setAttribute(ARIA_HIDDEN, "true");
    host.replaceChildren(
      heading,
      topSpacer,
      ...this.transcriptWindow.paragraphs.map(
        (paragraph) => this.renderTextParagraph(SOURCE_SURFACE, paragraph),
      ),
      bottomSpacer,
    );
    host.dataset.windowOffset = String(this.transcriptWindow.offset);
    host.dataset.windowTotal = String(this.transcriptWindow.total);
    host.dataset.mountedParagraphs = String(this.transcriptWindow.paragraphs.length);
  }

  private extendSourceDocument(
    paragraphs: ReadableParagraph[],
    direction: "prepend" | "append",
  ): void {
    const host = element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID);
    const topSpacer = host.querySelector<HTMLElement>(
      '[data-transcript-spacer="top"]',
    );
    const bottomSpacer = host.querySelector<HTMLElement>(
      '[data-transcript-spacer="bottom"]',
    );
    if (topSpacer === null || bottomSpacer === null) {
      this.renderSource();
      return;
    }
    const rows = paragraphs.map(
      (paragraph) => this.renderTextParagraph(SOURCE_SURFACE, paragraph),
    );
    if (direction === "prepend") {
      const firstMounted = topSpacer.nextSibling;
      for (const row of rows) host.insertBefore(row, firstMounted);
    } else {
      for (const row of rows) host.insertBefore(row, bottomSpacer);
    }
    topSpacer.style.height = `${this.transcriptWindow.topSpacerHeight()}px`;
    bottomSpacer.style.height = `${this.transcriptWindow.bottomSpacerHeight()}px`;
    host.dataset.windowOffset = String(this.transcriptWindow.offset);
    host.dataset.windowTotal = String(this.transcriptWindow.total);
    host.dataset.mountedParagraphs = String(this.transcriptWindow.paragraphs.length);
  }

  private renderNarration(paragraph: DraftEditorParagraph): HTMLElement {
    const row = document.createElement("section");
    row.className = "draft-copy-paragraph draft-narration-paragraph";
    row.dataset.editorParagraph = paragraph.paragraph_id;
    row.dataset.surface = DRAFT_SURFACE;
    row.dataset.paragraphKind = paragraph.kind;
    if (paragraph.block_id !== undefined) {
      row.dataset.blockId = paragraph.block_id;
    }
    const visibleSectionTitle = optionalDisplayTitle(paragraph.section_title);
    if (visibleSectionTitle !== null) {
      const sectionTitle = document.createElement("h3");
      sectionTitle.className = "draft-section-title";
      sectionTitle.textContent = visibleSectionTitle;
      row.append(sectionTitle);
    }
    const card = document.createElement("div");
    card.className = "draft-narration-card";
    const state = this.narrationEditorStates.get(paragraph.paragraph_id) ?? "reading";
    const narrationStatus = narrationStatusLabel(paragraph.narration_status);
    card.insertAdjacentHTML(
      "beforeend",
      state === "reading"
        ? '<p class="draft-narration-copy"></p><button type=BUTTON_ELEMENT data-narration-action="edit">修改</button><span class="draft-narration-drag-handle" data-narration-drag-handle="true" role=BUTTON_ELEMENT aria-label="拖动解说块">⋮⋮</span>'
        : state === "warning"
          ? '<p class="draft-narration-warning" role="alert">保存变化会创建不可变 child、清除旧录音绑定并回到待录音；重新录音、校对并绑定前不能生成粗剪。</p><div class="draft-narration-actions"><button type=BUTTON_ELEMENT class="primary" data-narration-action="continue">继续修改</button><button type=BUTTON_ELEMENT data-narration-action="cancel">取消</button></div>'
          : '<textarea rows="2" aria-label="解说文字"></textarea><div class="draft-narration-actions"><button type=BUTTON_ELEMENT class="primary" data-narration-action="save">保存</button><button type=BUTTON_ELEMENT data-narration-action="cancel">取消</button></div>',
    );
    row.append(card);
    const copy = card.querySelector<HTMLElement>(".draft-narration-copy");
    if (copy !== null) {
      copy.textContent = paragraph.text;
      const selected = this.decorationFor(
        DRAFT_SURFACE,
        paragraph.paragraph_id,
        paragraph.text,
      ).selected;
      copy.classList.toggle("is-selected", selected !== null && selected.end > selected.start);
    }
    const input = card.querySelector<HTMLTextAreaElement>("textarea");
    if (input !== null) input.value = paragraph.text;
    const label = document.createElement("span");
    label.className = "draft-narration-label";
    label.textContent = `解说 · ${narrationStatus}`;
    label.setAttribute(ARIA_LABEL, `解说，${narrationStatus}`);
    card.prepend(label);
    const disabled = this.view.locked || this.busy;
    row.querySelectorAll<HTMLButtonElement>(BUTTON_ELEMENT).forEach((button) => {
      button.disabled = disabled;
    });
    if (input !== null) input.disabled = disabled;
    return row;
  }

  private handleNarrationAction(button: HTMLButtonElement): void {
    const row = button.closest<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR);
    const paragraphId = row?.dataset.editorParagraph;
    const paragraph = paragraphId === undefined
      ? undefined
      : this.requireSnapshot().paragraphs.find((item) => item.paragraph_id === paragraphId);
    if (paragraph === undefined) return;
    const action = button.dataset.narrationAction;
    if (action === "save") {
      const input = row?.querySelector<HTMLTextAreaElement>("textarea");
      if (input !== null && input !== undefined) void this.saveNarration(paragraph, input.value);
      return;
    }
    if (action !== "edit" && action !== "continue" && action !== "cancel") return;
    if (action === "cancel") this.narrationEditorStates.delete(paragraph.paragraph_id);
    else {
      this.narrationEditorStates.set(
        paragraph.paragraph_id,
        action === "edit" && paragraph.narration_status === "recorded"
      ? WARNING_CLASS
          : "editing",
      );
    }
    this.replaceNarrationParagraph(paragraph);
  }

  private replaceNarrationParagraph(paragraph: DraftEditorParagraph): void {
    const row = [...element<HTMLElement>(DRAFT_DOCUMENT_ID)
      .querySelectorAll<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR)]
      .find((candidate) => candidate.dataset.editorParagraph === paragraph.paragraph_id);
    row?.replaceWith(this.renderNarration(paragraph));
    this.renderInteractionState();
  }

  private paragraphFor(
    surface: DraftEditorSurface,
    paragraphId: string,
  ): DraftEditorParagraph | ReadableParagraph | undefined {
    return surface === DRAFT_SURFACE
      ? this.requireSnapshot().paragraphs.find(
          (paragraph) => paragraph.paragraph_id === paragraphId,
        )
      : this.transcriptWindow.paragraphs.find(
          (paragraph) => paragraph.paragraph_id === paragraphId,
        );
  }

  private patchParagraphs(
    surface: DraftEditorSurface,
    paragraphIds: Iterable<string>,
  ): void {
    const host = element<HTMLElement>(
      surface === DRAFT_SURFACE ? DRAFT_DOCUMENT_ID : DRAFT_SOURCE_DOCUMENT_ID,
    );
    for (const paragraphId of new Set(paragraphIds)) {
      const current = [...host.querySelectorAll<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR)]
        .find((candidate) => candidate.dataset.editorParagraph === paragraphId);
      const paragraph = this.paragraphFor(surface, paragraphId);
      if (current === undefined || paragraph === undefined) continue;
      current.replaceWith(
        surface === DRAFT_SURFACE && "kind" in paragraph && paragraph.kind === NARRATION_KIND
          ? this.renderNarration(paragraph)
          : this.renderTextParagraph(surface, paragraph),
      );
    }
  }

  private selectionParagraphIds(surface: DraftEditorSurface): string[] {
    const active = this.view.selectionFor(surface);
    if (active === null) return [];
    return paragraphIdsForDisplayRange(
      active.response.resolved_display_range ?? active.response.display_range,
      this.paragraphOrder(surface),
    );
  }

  private correspondenceParagraphIds(): Set<string> {
    const correspondence = this.correspondences[this.correspondenceIndex];
    return correspondence === undefined
      ? new Set()
      : new Set([correspondence.paragraphId]);
  }

  private invalidatePuncSession(): void {
    this.punc = null;
    this.pPreview = null;
    this.pInput?.remove();
    this.pInput = null;
    this.pComposition = "";
    this.pStart = 0;
    this.pEnd = 0;
  }

  private applySnapshot(next: DraftEditorSnapshot): void {
    const previous = this.requireSnapshot();
    this.invalidatePuncSession();
    this.invalidateCaretResolve();
    this.view.setCaret(null);
    this.selectionGeneration.invalidate();
    this.selectionPending = false;
    this.selectionTimingStartedAt = null;
    this.narrationEditorStates.clear();
    this.snapshot = next;
    this.retryableWorkspaceMutation.clear();
    this.patchDraftSnapshot(previous, next);
    const title = element<HTMLElement>("draft-theme");
    title.textContent = optionalDisplayTitle(next.candidate.display_title) ?? "";
    title.hidden = title.textContent.length === 0;
  }

  private patchDraftSnapshot(
    previous: DraftEditorSnapshot,
    next: DraftEditorSnapshot,
  ): void {
    const before = previous.paragraphs;
    const after = next.paragraphs;
    const { prefixCount: prefix, suffixCount: suffix } = contiguousPatch(
      before,
      after,
      paragraphSignature,
    );

    for (let index = 0; index < prefix; index += 1) {
      this.retargetDraftParagraph(before[index]!, after[index]!);
    }
    for (let index = 0; index < suffix; index += 1) {
      this.retargetDraftParagraph(
        before[before.length - 1 - index]!,
        after[after.length - 1 - index]!,
      );
    }

    const host = element<HTMLElement>(DRAFT_DOCUMENT_ID);
    for (let index = prefix; index < before.length - suffix; index += 1) {
      host.children[prefix]?.remove();
    }
    const anchor = host.children[prefix] ?? null;
    for (const paragraph of after.slice(prefix, after.length - suffix)) {
      host.insertBefore(
        paragraph.kind === NARRATION_KIND
          ? this.renderNarration(paragraph)
          : this.renderTextParagraph(DRAFT_SURFACE, paragraph),
        anchor,
      );
    }
  }

  private retargetDraftParagraph(
    previous: DraftEditorParagraph,
    next: DraftEditorParagraph,
  ): void {
    const host = element<HTMLElement>(DRAFT_DOCUMENT_ID);
    const row = [...host.querySelectorAll<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR)]
      .find((candidate) => candidate.dataset.editorParagraph === previous.paragraph_id);
    if (row === undefined) return;
    row.dataset.editorParagraph = next.paragraph_id;
    row.querySelectorAll<HTMLElement>("[data-caret-paragraph]").forEach((hit) => {
      hit.dataset.caretParagraph = next.paragraph_id;
    });
    row.querySelectorAll<HTMLElement>(
      ".is-selected, .is-correspondence, .is-search-match, .is-current-search",
    ).forEach(
      (fragment) => fragment.classList.remove(
        "is-selected",
        "is-correspondence",
        "is-search-match",
        "is-current-search",
      ),
    );
    row.querySelectorAll<HTMLElement>(".draft-insertion-caret").forEach(
      (caret) => caret.remove(),
    );
  }

  private renderTextParagraph(
    surface: DraftEditorSurface,
    paragraph: DraftEditorParagraph | ReadableParagraph,
  ): HTMLElement {
    const row = document.createElement("section");
    row.className = "draft-copy-paragraph";
    row.dataset.editorParagraph = paragraph.paragraph_id;
    row.dataset.surface = surface;
    if ("kind" in paragraph) row.dataset.paragraphKind = paragraph.kind;
    if ("block_id" in paragraph && paragraph.block_id !== undefined) {
      row.dataset.blockId = paragraph.block_id;
    }

    const visibleSectionTitle = SECTION_TITLE_KIND in paragraph
      ? optionalDisplayTitle(paragraph.section_title)
      : null;
    if (visibleSectionTitle !== null) {
      const sectionTitle = document.createElement("h3");
      sectionTitle.className = "draft-section-title";
      sectionTitle.textContent = visibleSectionTitle;
      row.append(sectionTitle);
    }

    const person = "person" in paragraph
      ? paragraph.person
      : {
          name: paragraph.person_name,
          role: null,
          local_speaker_id: paragraph.local_speaker_id,
        };
    const bylineText = visibleBylineForSurface(surface, person);
    const byline = bylineText === null ? null : document.createElement("span");
    if (byline !== null) {
      byline.className = "draft-byline";
      byline.textContent = bylineText;
    }

    const text = document.createElement("span");
    text.className = "draft-paragraph-text";
    text.dataset.editorText = "true";
    const localPreview = surface === DRAFT_SURFACE
      && this.pPreview?.paragraphId === paragraph.paragraph_id
      ? this.pPreview.text
      : null;
    const value = localPreview ?? paragraph.text;
    const decoration = this.decorationFor(surface, paragraph.paragraph_id, value);
    const sourceRuns = "source_runs" in paragraph && localPreview === null
      ? paragraph.source_runs
      : [];
    decoration.boundaries = sourceRuns.map((run) => run.start_offset);
    const runs = partitionText(value, paragraph.paragraph_id, decoration);
    appendDraftTextRuns(text, value, runs, () => this.caretElement());
    if (sourceRuns.length > 0) {
      const fragments = [...text.querySelectorAll<HTMLElement>("[data-text-fragment='true']")];
      for (const fragment of fragments) {
        const start = Number(fragment.dataset.utf16Start ?? 0);
        const run = sourceRuns.find((candidate) =>
          candidate.start_offset <= start && start < candidate.end_offset,
        );
        if (run !== undefined && run.block_id !== undefined) fragment.dataset.blockId = run.block_id;
      }
    }
    if (byline !== null) row.append(byline);
    row.append(text);
    return row;
  }

  private decorationFor(
    surface: DraftEditorSurface,
    paragraphId: string,
    text: string,
  ): TextDecoration {
    const active = this.view.selectionFor(surface);
    const order = this.paragraphOrder(surface);
    const activeRange = active === null
      ? null
      : displayRangeForParagraph(
          active.response.resolved_display_range ?? active.response.display_range,
          paragraphId,
          text,
          order,
        );
    const selected = activeRange;
    const matches = this.search[surface].matches
      .map((match, index) => ({
        match,
        index,
      }))
      .filter(({ match }) => match.paragraph_id === paragraphId)
      .map(({ match, index }) => ({
        start: match.start_offset,
        end: match.end_offset,
        current: index === this.search[surface].current,
      }));
    const activeCorrespondence = this.correspondences[this.correspondenceIndex];
    const correspondence = surface === SOURCE_SURFACE
      && activeCorrespondence?.paragraphId === paragraphId
      ? {
          paragraphId: activeCorrespondence.paragraphId,
          start: activeCorrespondence.start,
          end: activeCorrespondence.end,
        }
      : null;
    let caret = surface === DRAFT_SURFACE
      ? caretOffsetForParagraph(this.view.caret, paragraphId)
      : null;
    if (
      surface === DRAFT_SURFACE
      && this.pPreview?.paragraphId === paragraphId
    ) {
      try {
        caret = utf16ToCodePoint(text, this.pStart);
      } catch {
        caret = null;
      }
    }
    return {
      selected,
      matches,
      correspondence,
      playback: null,
      caret,
    };
  }

  private paragraphOrder(surface: DraftEditorSurface): string[] {
    if (surface === DRAFT_SURFACE) {
      return this.requireSnapshot().paragraphs.map((paragraph) => paragraph.paragraph_id);
    }
    return this.transcriptWindow.paragraphs.map((paragraph) => paragraph.paragraph_id);
  }

  private caretElement(): HTMLElement {
    const caret = document.createElement("span");
    caret.className = "draft-insertion-caret";
    const activeCaret = this.view.caret;
    if (activeCaret !== null) caret.dataset.boundaryId = activeCaret.boundary_id;
    caret.setAttribute(ARIA_HIDDEN, "true");
    return caret;
  }

  private async handleDocumentPointerUp(surface: DraftEditorSurface): Promise<void> {
    if (!await this.flushPunc()) return;
    const selection = window.getSelection();
    const wasDragging = this.pointerGesture.state() === "dragging";
    const completed = this.pointerGesture.completePointerUp(
      surface,
      selection !== null && !selection.isCollapsed,
    );
    if (this.pointerGesture.consumedSelectionClick(surface)) {
      selection?.removeAllRanges();
      return;
    }
    if (completed && !wasDragging) {
      if (surface === DRAFT_SURFACE && selection !== null && !selection.isCollapsed) {
        const anchor = endpointFromDom(selection.anchorNode, selection.anchorOffset, surface);
        const focus = endpointFromDom(selection.focusNode, selection.focusOffset, surface);
        if (anchor !== null && focus !== null && this.startPunc(anchor, focus)) {
          selection.removeAllRanges();
          return;
        }
      }
      void this.captureSelection(surface);
    }
  }

  private handleDragPointerDown(surface: DraftEditorSurface, event: PointerEvent): void {
    if (this.view.draftMode !== "body" || this.view.locked || this.busy) return;
    if (this.punc?.changed) {
      // Start the punctuation save, but keep the gesture pipeline intact:
      // preventing default or returning here would discard the current
      // pointerdown (no selection anchor, no drag).  The save completes on
      // the shared pFlush promise before the drop executes.
      void this.flushPunc();
    } else if (this.punc !== null) {
      this.closePuncSession(this.punc);
    }
    const selection = this.view.selectionFor(surface);
    const point = endpointFromPoint(event.clientX, event.clientY, surface)
      ?? endpointFromRenderedFragments(event.clientX, event.clientY, surface);
    const narrationDragHandle = (event.target as HTMLElement | null | undefined)
      ?.closest<HTMLElement>("[data-narration-drag-handle]") ?? null;
    const narrationRow = narrationDragHandle?.closest<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR) ?? null;
    const narrationBlockId = narrationRow?.dataset.blockId ?? null;
    // Only a completed resolve (view selection) or the narration drag handle
    // can be the business drag source.  A native selection still awaiting
    // resolve is not a draggable object: the first gesture only establishes
    // the selection, and the second pointer sequence drags it.
    const withinSelection = narrationDragHandle !== null
      || selection !== null
        && point !== null
        && this.displayPointInSelection(
          surface,
          point,
          selection.response.resolved_display_range ?? selection.response.display_range,
        );
    this.pointerGesture.pointerDown(
      surface,
      { x: event.clientX, y: event.clientY },
      { withinSelection },
    );
    this.draftDragGeneration += 1;
    this.draftDrag = {
      surface,
      target: null,
      blockId: null,
      narrationBlockId,
      dragging: false,
      pointerTarget: (event.target as HTMLElement | null | undefined)?.closest<HTMLElement>(
        "[data-editor-text], [data-editor-paragraph]",
      ) ?? (event.currentTarget as HTMLElement | null | undefined) ?? null,
      pointerId: Number.isFinite(event.pointerId) ? event.pointerId : null,
      generation: this.draftDragGeneration,
      frozen: null,
    };
  }

  private handleDragPointerMove(event: PointerEvent): void {
    const drag = this.draftDrag;
    if (drag === null) return;
    const phase = this.pointerGesture.pointerMove({ x: event.clientX, y: event.clientY });
    // A plain text selection gesture stays a selection; only a drag that
    // started inside a resolved active selection (or from a narration drag
    // handle) reaches the dragging phase and may freeze a drop.
    if (phase !== "dragging") return;
    if (!drag.dragging) {
      drag.frozen = this.freezeDraftDrop(
        drag.surface,
        drag.generation,
        drag.narrationBlockId,
      );
      if (drag.frozen === null) {
        this.cancelDrag();
        return;
      }
      drag.dragging = true;
      event.preventDefault();
      if (typeof window !== "undefined") window.getSelection()?.removeAllRanges();
      this.capturePointer(drag.pointerTarget, drag.pointerId);
      this.setBusinessDragState(true);
    }
    event.preventDefault();
    this.autoScrollDraft(event.clientX, event.clientY);
    const hitTestStartedAt = performance.now();
    const target = this.resolveDraftDropTarget(event.clientX, event.clientY);
    this.editPerformanceTrace.record(
      POINTERMOVE_HIT_TEST_PHASE,
      performance.now() - hitTestStartedAt,
    );
    if (target === null) {
      drag.target = null;
      drag.blockId = null;
      this.clearDropIndicator();
      element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset.dropTarget = "invalid";
      return;
    }
    drag.target = target.point;
    drag.blockId = target.blockId;
    this.renderDropIndicator(target.point);
    element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset.dropTarget = target.point.paragraph_id;
  }

  private resolveDraftDropTarget(
    x: number,
    y: number,
  ): { point: DraftEditorRequestPoint; blockId: string } | null {
    const point = endpointFromPoint(x, y, DRAFT_SURFACE)
      ?? endpointFromRenderedFragments(x, y, DRAFT_SURFACE);
    if (point === null) return null;
    const paragraph = this.requireSnapshot().paragraphs.find(
      (candidate) => candidate.paragraph_id === point.paragraph_id,
    );
    if (paragraph === undefined) return null;
    let characterOffset: number;
    try {
      characterOffset = utf16ToCodePoint(paragraph.text, point.offset);
    } catch {
      return null;
    }
    try {
      return {
        point,
        blockId: this.schema2DisplayPoint({
          paragraph_id: point.paragraph_id,
          character_offset: characterOffset,
          utf16_offset: point.offset,
        }).block_id,
      };
    } catch {
      return null;
    }
  }

  private freezeDraftDrop(
    surface: DraftEditorSurface,
    generation: number,
    narrationBlockId: string | null = null,
  ): FrozenDraftDrop | null {
    const selection = this.view.selectionFor(surface);
    const snapshot = this.requireSnapshot();
    if (surface === DRAFT_SURFACE && narrationBlockId !== null) {
      // The narration drag handle was pressed: the narration block is the
      // dragged object regardless of any existing selection.  A selection
      // may exist from the draft pane, so the handle press must win.
      return this.freezeNarrationFromSnapshot(
        snapshot,
        generation,
        narrationBlockId,
      );
    }
    const narrationSelection = selection?.response.narration_block_id != null;
    if (
      selection === null
      || selection.response.candidate_id !== snapshot.candidate.candidate_id
      || selection.response.resolution.degraded && !selection.acceptedDegraded
      || (!narrationSelection && selection.response.resolution.refs.length === 0)
    ) {
      if (selection?.response.resolution.degraded && !selection.acceptedDegraded) {
        this.degradedPromptOperation = surface === DRAFT_SURFACE ? "move" : "insert";
        this.renderDegradedPrompt();
      }
      this.onStatus("没有可移动的选区或解说块。", true);
      return null;
    }
    const resolution = selection.response.resolution;
    const basis = copyDraftWorkspaceBasis(snapshot.workspace);
    const operation = surface === DRAFT_SURFACE ? "move_selection" : "insert_source_refs";
    const resolutionHash = selection.response.resolution_hash;
    if (surface === DRAFT_SURFACE && resolutionHash === undefined) {
      this.onStatus("选区已过期，请重新拖选。", true);
      return null;
    }
    const frozenResolutionHash = resolutionHash ?? "";
    if (surface === DRAFT_SURFACE) {
      const range = selection.response.display_range;
      const order = this.paragraphOrder(DRAFT_SURFACE);
      const anchorIndex = order.indexOf(range.anchor.paragraph_id);
      const focusIndex = order.indexOf(range.focus.paragraph_id);
      if (anchorIndex < 0 || focusIndex < 0) return null;
      const start = Math.min(anchorIndex, focusIndex);
      const end = Math.max(anchorIndex, focusIndex);
      const kinds = new Set(
        this.requireSnapshot().paragraphs
          .slice(start, end + 1)
          .map((paragraph) => paragraph.kind),
      );
      const narration = selection.response.narration_block_id;
      if (narration !== null && narration !== undefined) {
        if (
          kinds.size !== 1
          || !kinds.has(NARRATION_KIND)
          || range.anchor.paragraph_id !== range.focus.paragraph_id
        ) {
          this.onStatus("解说只能作为完整块移动，不能与其他正文混选。", true);
          return null;
        }
        return {
          generation,
          operation_id: draftWorkspaceOperationId(basis.expected_checkpoint_ref.generation),
          basis,
          candidate_id: snapshot.candidate.candidate_id,
          operation,
          source: {
            kind: "narration_block",
            surface: DRAFT_SURFACE,
            block_id: narration,
            text: selection.response.narration_text ?? resolution.canonical_text,
            status: selection.response.narration_status ?? DRAFT_SURFACE,
            recorded_refs: resolution.refs.map(schema2Ref),
          },
          accept_degraded: resolution.degraded && selection.acceptedDegraded,
          resolvedRange: copyDisplayRange(
            selection.response.resolved_display_range ?? range,
          ),
        };
      }
      if (kinds.has(NARRATION_KIND) || kinds.has(SECTION_TITLE_KIND)) {
        this.onStatus("正文拖放不能跨解说或章节标题。", true);
        return null;
      }
      const blockIds = this.selectionBlockIds(selection.response);
      if (blockIds.length === 0) return null;
      return {
        generation,
        operation_id: draftWorkspaceOperationId(basis.expected_checkpoint_ref.generation),
        basis,
        candidate_id: snapshot.candidate.candidate_id,
        operation,
        source: {
          kind: "resolved_selection",
          resolution_hash: frozenResolutionHash,
          surface: DRAFT_SURFACE,
          selection_kind: SOURCE_EXCERPT_KIND,
          display_range: {
            anchor: this.schema2DisplayPoint(range.anchor),
            focus: this.schema2DisplayPoint(range.focus),
          },
          block_ids: blockIds,
          refs: resolution.refs.map(schema2Ref),
          canonical_text: resolution.canonical_text,
          degraded: resolution.degraded,
        },
        accept_degraded: resolution.degraded && selection.acceptedDegraded,
        resolvedRange: copyDisplayRange(
          selection.response.resolved_display_range ?? range,
        ),
      };
    }
    const first = resolution.refs[0];
    if (first === undefined) return null;
    if (resolution.refs.some((ref) =>
      ref.source_id !== first.source_id
      || ref.transcript_version_id !== first.transcript_version_id
    )) {
      this.onStatus("原稿选区不能跨素材绑定。", true);
      return null;
    }
    return {
      generation,
      operation_id: draftWorkspaceOperationId(basis.expected_checkpoint_ref.generation),
      basis,
      candidate_id: snapshot.candidate.candidate_id,
      operation,
      source: {
        kind: "exact_source_refs",
        source_id: first.source_id,
        transcript_version_id: first.transcript_version_id,
        refs: resolution.refs.map(schema2Ref),
        canonical_text: resolution.canonical_text,
      },
      accept_degraded: resolution.degraded && selection.acceptedDegraded,
      resolvedRange: null,
    };
  }

  private freezeNarrationFromSnapshot(
    snapshot: DraftEditorSnapshot,
    generation: number,
    narrationBlockId: string,
  ): FrozenDraftDrop | null {
    // A clean page has no selection; the drag handle is the only entry for
    // moving a narration block (spec: narration moves start from the wide
    // drag zone).  Build the narration envelope directly from the snapshot
    // instead of requiring a prior text selection.
    const paragraph = snapshot.paragraphs.find(
      (item) => item.kind === NARRATION_KIND && item.block_id === narrationBlockId,
    );
    const block = (snapshot.blocks ?? []).find(
      (item) => item.kind === NARRATION_KIND && item.block_id === narrationBlockId,
    );
    if (paragraph === undefined || block === undefined || block.kind !== NARRATION_KIND) {
      this.onStatus("解说块已不存在，请刷新后重试。", true);
      return null;
    }
    const basis = copyDraftWorkspaceBasis(snapshot.workspace);
    const textLength = Array.from(paragraph.text).length;
    const utf16Length = paragraph.text.length;
    return {
      generation,
      operation_id: draftWorkspaceOperationId(basis.expected_checkpoint_ref.generation),
      basis,
      candidate_id: snapshot.candidate.candidate_id,
      operation: "move_selection",
      source: {
        kind: "narration_block",
        surface: DRAFT_SURFACE,
        block_id: block.block_id,
        text: block.text,
        status: block.status,
        recorded_refs: block.recorded_refs,
      },
      accept_degraded: false,
      resolvedRange: {
        anchor: {
          paragraph_id: paragraph.paragraph_id,
          character_offset: 0,
          utf16_offset: 0,
        },
        focus: {
          paragraph_id: paragraph.paragraph_id,
          character_offset: textLength,
          utf16_offset: utf16Length,
        },
      },
    };
  }

  private renderDropIndicator(point: DraftEditorRequestPoint): void {
    const key = `${point.paragraph_id}:${point.offset}`;
    if (this.draftDropIndicator !== null && this.draftDropIndicatorKey === key) return;
    this.clearDraftDropIndicator();
    const host = element<HTMLElement>(DRAFT_DOCUMENT_ID);
    const row = [...host.querySelectorAll<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR)]
      .find((candidate) => candidate.dataset.editorParagraph === point.paragraph_id);
    if (row === undefined) return;
    const indicator = document.createElement("span");
    indicator.className = "draft-drop-indicator";
    indicator.dataset.draftDropIndicator = "true";
    indicator.setAttribute(ARIA_HIDDEN, "true");
    const fragments = [...row.querySelectorAll<HTMLElement>('[data-text-fragment="true"]')];
    const fragment = fragments.find((candidate, index) => {
      const start = Number(candidate.dataset.utf16Start ?? candidate.dataset.start);
      const text = candidate.textContent ?? "";
      const end = start + text.length;
      return Number.isInteger(start)
        && start <= point.offset
        && (point.offset < end || (point.offset === end && index === fragments.length - 1));
    });
    let rect: DOMRect | null = null;
    if (fragment !== undefined && fragment.firstChild?.nodeType === 3) {
      const start = Number(fragment.dataset.utf16Start ?? fragment.dataset.start);
      const localOffset = point.offset - start;
      const range = document.createRange();
      range.setStart(
        fragment.firstChild,
        Math.max(0, Math.min(localOffset, fragment.firstChild.textContent?.length ?? 0)),
      );
      range.collapse(true);
      rect = range.getBoundingClientRect();
    } else if (fragments.length === 0 && row.dataset.paragraphKind === SECTION_TITLE_KIND) {
      rect = row.querySelector<HTMLElement>(".draft-section-title")?.getBoundingClientRect() ?? null;
    } else {
      return;
    }
    if (rect === null || rect.bottom <= rect.top) return;
    const hostRect = host.getBoundingClientRect();
    indicator.style.left = `${rect.left - hostRect.left + host.scrollLeft}px`;
    indicator.style.top = `${rect.top - hostRect.top + host.scrollTop}px`;
    indicator.style.height = `${Math.max(18, rect.height)}px`;
    host.append(indicator);
    row.dataset.dropTarget = "true";
    this.draftDropIndicator = indicator;
    this.draftDropIndicatorRow = row;
    this.draftDropIndicatorKey = key;
  }

  private clearDropIndicator(): void {
    this.clearDraftDropIndicator();
  }

  private clearDraftDropIndicator(): void {
    this.draftDropIndicator?.remove();
    this.draftDropIndicator = null;
    this.draftDropIndicatorKey = null;
    if (this.draftDropIndicatorRow !== null) {
      delete this.draftDropIndicatorRow.dataset.dropTarget;
      this.draftDropIndicatorRow = null;
    }
  }

  private autoScrollDraft(x: number, y: number): void {
    const host = element<HTMLElement>(DRAFT_DOCUMENT_ID);
    const rect = host.getBoundingClientRect();
    const edge = 48;
    let delta = 0;
    if (y < rect.top + edge) delta = -Math.max(4, Math.round((rect.top + edge - y) / 4));
    else if (y > rect.bottom - edge) delta = Math.max(4, Math.round((y - (rect.bottom - edge)) / 4));
    if (delta !== 0) host.scrollTop += delta;
    void x;
  }

  private async handleDragPointerUp(_event: PointerEvent): Promise<void> {
    const drag = this.draftDrag;
    this.draftDrag = null;
    const result = this.pointerGesture.pointerUp();
    this.releasePointer(drag?.pointerTarget, drag?.pointerId);
    if (drag?.dragging) this.setBusinessDragState(false);
    this.clearDropIndicator();
    delete element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset.dropTarget;
    if (drag === null) return;
    const frozen = drag.frozen;
    if (frozen === null) return;
    if (result !== "drag" && !drag.dragging) return;
    const target = drag.target;
    const blockId = drag.blockId;
    if (target === null || blockId === null) return;
    if (this.view.locked || !await this.flushPunc() || this.busy) return;
    if (
      drag.surface === DRAFT_SURFACE
      && this.displayPointInSelection(
        DRAFT_SURFACE,
        { paragraph_id: target.paragraph_id, offset: target.offset, offset_encoding: "utf16" },
        frozen.resolvedRange!,
      )
    ) {
      this.onStatus("已回到原选区，移动已取消。", false);
      return;
    }
    this.pendingDraftDrop = frozen;
    this.busy = true;
    this.renderInteractionState();
    try {
      const requestStartedAt = performance.now();
      const response = await this.api<EditResponse>(
        DRAFT_EDIT_API,
        post({
          ...frozen.basis,
          operation_id: frozen.operation_id,
          schema_version: 2,
          operation: frozen.operation,
          accept_degraded: frozen.accept_degraded,
          source: frozen.source,
          target: { paragraph_id: target.paragraph_id, block_id: blockId, utf16_offset: target.offset },
        }),
      );
      this.editPerformanceTrace.record(
        DROP_HTTP_SERVER_PHASE,
        performance.now() - requestStartedAt,
      );
      if (!this.draftDropIsCurrent(frozen)) return;
      if (response.timing !== undefined) {
        Object.assign(
          element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset,
          serverDraftEditTimingDataset(
            drag.surface === DRAFT_SURFACE ? "move" : "insert",
            response.timing,
          ),
        );
      }
      const domPatchStartedAt = performance.now();
      this.view.clearSelections();
      this.view.setCaret(null);
      this.applySnapshot(response.draft_editor);
      const resultSelection = response.result_selection;
      if (
        resultSelection !== undefined
        && resultSelection.response.candidate_id === response.draft_editor.candidate.candidate_id
      ) {
        this.view.setSelection(DRAFT_SURFACE, {
          surface: resultSelection.surface,
          request: resultSelection.request,
          response: resultSelection.response,
          acceptedDegraded: resultSelection.accepted_degraded,
        });
        this.patchParagraphs(DRAFT_SURFACE, this.selectionParagraphIds(DRAFT_SURFACE));
        this.correspondences = draftCorrespondences(resultSelection.response);
        this.correspondenceIndex = 0;
        this.renderCorrespondenceNavigation();
        this.preparePlayback(resultSelection.response.resolution.refs);
      } else {
        this.correspondences = [];
        this.correspondenceIndex = 0;
        this.playback.reset();
        this.renderCorrespondenceNavigation();
        this.renderPlayer();
      }
      this.editPerformanceTrace.record(
        DOM_PATCH_PHASE,
        performance.now() - domPatchStartedAt,
      );
      this.publishEditPerformance(drag.surface === DRAFT_SURFACE ? "move" : "insert");
      const action = drag.surface === DRAFT_SURFACE ? "正文已移动" : "原稿内容已插入正文";
      this.onStatus(`${action}。`);
    } catch (error) {
      if (isUncertainDraftDropFailure(error)) {
        this.lock("uncertain");
      } else if (this.draftDropIsCurrent(frozen)) {
        const message = (error as { message?: unknown }).message;
        if ((error as { code?: unknown }).code === INVALID_WORKFLOW_CHANGE) {
          if (
            typeof message === "string"
            && (message.includes("cannot uniquely retain display punctuation")
              || message.includes("punctuation prefix orphaned")
              || message.includes("punctuation suffix orphaned"))
          ) {
            this.onStatus("当前选区两侧的人工标点无法唯一保留；请把相关标点一并选中，或先调整标点后再移动。", true);
          } else if (typeof message === "string" && message.includes("out of bounds")) {
            this.onStatus("落点超出有效范围，内容未更改；请拖到另一处再试。", true);
          } else {
            this.onStatus("这个落点没有产生有效调整，内容未更改；请拖到另一处再试", true);
          }
        } else {
          this.handleError(error);
        }
      }
    } finally {
      if (this.pendingDraftDrop === frozen) {
        this.pendingDraftDrop = null;
        this.busy = false;
        this.renderInteractionState();
      }
    }
  }

  private cancelDrag(): void {
    if (this.pendingDraftDrop !== null) return;
    const drag = this.draftDrag;
    this.draftDragGeneration += 1;
    this.draftDrag = null;
    this.pointerGesture.cancel();
    this.releasePointer(drag?.pointerTarget, drag?.pointerId);
    if (drag?.dragging) this.setBusinessDragState(false);
    this.clearDropIndicator();
    delete element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset.dropTarget;
  }

  private handleDragPointerCancel(_event: PointerEvent): void {
    if (this.pendingDraftDrop !== null) return;
    this.cancelDrag();
  }

  private capturePointer(target: HTMLElement | null, pointerId: number | null): void {
    if (target === null || pointerId === null || typeof target.setPointerCapture !== FUNCTION_TYPE) return;
    try {
      target.setPointerCapture(pointerId);
    } catch {
      // The browser may have already cancelled the pointer sequence.
    }
  }

  private releasePointer(target: HTMLElement | null | undefined, pointerId: number | null | undefined): void {
    if (target === null || target === undefined || pointerId === null || pointerId === undefined) return;
    if (typeof target.releasePointerCapture !== FUNCTION_TYPE) return;
    try {
      if (typeof target.hasPointerCapture !== FUNCTION_TYPE || target.hasPointerCapture(pointerId)) {
        target.releasePointerCapture(pointerId);
      }
    } catch {
      // The browser may have already released the capture.
    }
  }

  private setBusinessDragState(active: boolean): void {
    const shell = element<HTMLElement>(DRAFT_EDITOR_SHELL_ID);
    if (active) shell.dataset.businessDragging = "true";
    else delete shell.dataset.businessDragging;
    shell.classList.toggle("is-business-dragging", active);
    if (!active && typeof window !== "undefined") window.getSelection()?.removeAllRanges();
  }

  private draftDropIsCurrent(frozen: FrozenDraftDrop): boolean {
    const snapshot = this.requireSnapshot();
    return this.pendingDraftDrop === frozen
      && this.draftDragGeneration === frozen.generation
      && snapshot.candidate.candidate_id === frozen.candidate_id
      && JSON.stringify(snapshot.workspace) === JSON.stringify(frozen.basis);
  }

  private displayPointInSelection(
    surface: DraftEditorSurface,
    point: DraftEditorRequestPoint,
    range: DraftEditorSelectionResponse["display_range"],
  ): boolean {
    const order = this.paragraphOrder(surface);
    const position = order.indexOf(point.paragraph_id);
    const anchor = order.indexOf(range.anchor.paragraph_id);
    const focus = order.indexOf(range.focus.paragraph_id);
    if (position < 0 || anchor < 0 || focus < 0) return false;
    const start = anchor < focus || (anchor === focus && range.anchor.character_offset <= range.focus.character_offset)
      ? range.anchor
      : range.focus;
    const end = start === range.anchor ? range.focus : range.anchor;
    const startIndex = order.indexOf(start.paragraph_id);
    const endIndex = order.indexOf(end.paragraph_id);
    if (position < startIndex || position > endIndex) return false;
    const offset = point.offset_encoding === "utf16"
      ? point.offset
      : codePointToUtf16(
        this.paragraphFor(surface, point.paragraph_id)?.text ?? "",
        point.offset,
      );
    const startOffset = point.paragraph_id === start.paragraph_id ? start.utf16_offset : 0;
    const endText = this.paragraphFor(surface, end.paragraph_id)?.text ?? "";
    const endOffset = point.paragraph_id === end.paragraph_id
      ? end.utf16_offset
      : codePointToUtf16(endText, Array.from(endText).length);
    return point.paragraph_id !== start.paragraph_id || offset >= startOffset
      ? point.paragraph_id !== end.paragraph_id || offset <= endOffset
      : false;
  }

  private selectionBlockIds(selection: DraftEditorSelectionResponse): string[] {
    const paragraphs = this.requireSnapshot().paragraphs;
    const order = paragraphs.map((paragraph) => paragraph.paragraph_id);
    const anchor = selection.display_range.anchor;
    const focus = selection.display_range.focus;
    const anchorIndex = order.indexOf(anchor.paragraph_id);
    const focusIndex = order.indexOf(focus.paragraph_id);
    if (anchorIndex < 0 || focusIndex < 0) return [];
    const [start, end] = anchorIndex < focusIndex
      || (anchorIndex === focusIndex && anchor.character_offset <= focus.character_offset)
      ? [anchor, focus]
      : [focus, anchor];
    const startIndex = order.indexOf(start.paragraph_id);
    const endIndex = order.indexOf(end.paragraph_id);
    const ids: string[] = [];
    for (let index = startIndex; index <= endIndex; index += 1) {
      const paragraph = paragraphs[index]!;
      if (paragraph.kind !== SOURCE_EXCERPT_KIND) continue;
      const from = paragraph.paragraph_id === start.paragraph_id ? start.character_offset : 0;
      const to = paragraph.paragraph_id === end.paragraph_id
        ? end.character_offset
        : Array.from(paragraph.text).length;
      for (const run of paragraph.source_runs) {
        if (run.block_id === undefined || run.end_offset <= from || to <= run.start_offset) continue;
        if (ids[ids.length - 1] !== run.block_id) ids.push(run.block_id);
      }
    }
    return ids;
  }

  private schema2DisplayPoint(
    point: DraftEditorSelectionResponse["display_range"]["anchor"],
  ): { paragraph_id: string; block_id: string; utf16_offset: number } {
    const paragraph = this.requireSnapshot().paragraphs.find((item) => item.paragraph_id === point.paragraph_id);
    let blockId = paragraph?.block_id;
    if (paragraph?.kind === SOURCE_EXCERPT_KIND) {
      for (let index = 0; index < paragraph.source_runs.length; index += 1) {
        const run = paragraph.source_runs[index]!;
        if (run.start_offset <= point.character_offset && point.character_offset < run.end_offset) {
          blockId = run.block_id;
          break;
        }
        if (point.character_offset === run.end_offset) {
          const next = paragraph.source_runs[index + 1];
          if (next === undefined || next.start_offset !== point.character_offset) {
            blockId = run.block_id;
            break;
          }
        }
      }
    }
    if (blockId === undefined) throw new Error("目标段落缺少 block identity");
    return { paragraph_id: point.paragraph_id, block_id: blockId, utf16_offset: point.utf16_offset };
  }

  private startPunc(
    point: DraftEditorRequestPoint,
    endPoint: DraftEditorRequestPoint = point,
  ): boolean {
    if (this.view.draftMode !== "body" || this.view.locked || this.busy) return false;
    const snapshot = this.requireSnapshot();
    const paragraph = snapshot.paragraphs.find(
      (candidate) => candidate.paragraph_id === point.paragraph_id,
    );
    if (
      paragraph?.kind !== SOURCE_EXCERPT_KIND
      || endPoint.paragraph_id !== point.paragraph_id
    ) return false;
    let characterOffset: number;
    let endCharacterOffset: number;
    try {
      characterOffset = utf16ToCodePoint(paragraph.text, point.offset);
      endCharacterOffset = utf16ToCodePoint(paragraph.text, endPoint.offset);
    } catch {
      return false;
    }
    let startPoint = point;
    let endPointOrdered = endPoint;
    if (characterOffset > endCharacterOffset) {
      [characterOffset, endCharacterOffset] = [endCharacterOffset, characterOffset];
      [startPoint, endPointOrdered] = [endPoint, point];
    }
    const blockId = sourceBlockAtDisplayOffset(paragraph.source_runs, characterOffset);
    if (blockId === null) return false;
    if (characterOffset !== endCharacterOffset) {
      const selected = Array.from(paragraph.text)
        .slice(characterOffset, endCharacterOffset)
        .join("");
      if (
        selected.length === 0
        || Array.from(selected).some((character) => !/^\p{P}$/u.test(character))
        || sourceBlockAtDisplayOffset(paragraph.source_runs, endCharacterOffset - 1) !== blockId
      ) return false;
    }
    const binding: PunctuationSessionBinding = {
      candidateId: snapshot.candidate.candidate_id,
      checkpoint: { ...snapshot.workspace.expected_checkpoint_ref },
      currentCandidate: { ...snapshot.workspace.expected_current_candidate_ref },
      paragraphId: point.paragraph_id,
      blockId,
      startUtf16Offset: startPoint.offset,
      endUtf16Offset: endPointOrdered.offset,
    };
    try {
      this.punc = new PuncSession(binding, paragraph.text);
    } catch {
      return false;
    }
    this.pStart = startPoint.offset;
    this.pEnd = endPointOrdered.offset;
    this.pPreview = {
      paragraphId: point.paragraph_id,
      text: paragraph.text,
    };
    this.mountPuncInput();
    this.patchParagraphs(DRAFT_SURFACE, [point.paragraph_id]);
    return true;
  }

  private mountPuncInput(): void {
    this.pInput?.remove();
    const input = document.createElement(INPUT_ELEMENT);
    input.type = "text";
    input.className = "draft-punctuation-input";
    input.setAttribute(ARIA_LABEL, "标点输入");
    input.autocomplete = "off";
    input.spellcheck = false;
    input.value = "";
    input.addEventListener("beforeinput", (event) => {
      this.beforePunc(event as InputEvent);
    });
    input.addEventListener("paste", (event) => {
      event.preventDefault();
      const value = event.clipboardData?.getData("text/plain") ?? "";
      this.applyPunc(value, "paste");
    });
    input.addEventListener("compositionstart", () => {
      this.pComposition = "";
    });
    input.addEventListener("compositionupdate", (event) => {
      this.pComposition = event.data;
    });
    input.addEventListener("compositionend", (event) => {
      const value = event.data || this.pComposition;
      this.pComposition = "";
      if (value.length > 0) this.applyPunc(value, "composition");
    });
    input.addEventListener("keydown", (event) => {
      this.keyPunc(event);
    });
    input.addEventListener(INPUT_ELEMENT, () => {
      // beforeinput is the only input path.  Keep the browser-controlled
      // field empty so it can never become a second body editor.
      input.value = "";
    });
    element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).append(input);
    this.pInput = input;
    input.focus({ preventScroll: true });
  }

  private beforePunc(event: InputEvent): void {
    if (this.punc === null) return;
    const inputType = event.inputType;
    if (inputType === "insertCompositionText") {
      event.preventDefault();
      this.pComposition = event.data ?? "";
      return;
    }
    if (inputType === "insertFromPaste") {
      event.preventDefault();
      return;
    }
    if (inputType === "deleteContentBackward") {
      event.preventDefault();
      this.deletePunc(false);
      return;
    }
    if (inputType === "deleteContentForward") {
      event.preventDefault();
      this.deletePunc(true);
      return;
    }
    if (
      inputType === "insertText"
      || inputType === "insertReplacementText"
    ) {
      event.preventDefault();
      this.applyPunc(event.data ?? "", "typing");
    }
  }

  private keyPunc(event: KeyboardEvent): void {
    if (this.punc === null) return;
    if (event.key === "Escape") {
      event.preventDefault();
      this.cancelPunc();
      return;
    }
    if (event.key === "Backspace" || event.key === "Delete") {
      event.preventDefault();
      this.deletePunc(event.key === "Delete");
      return;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      const from = event.currentTarget as HTMLElement | null;
      const tabStops = this.tabbableElements();
      const fromIndex = from === null ? -1 : tabStops.indexOf(from);
      void this.flushPunc().then((saved) => {
        if (!saved || this.punc !== null || fromIndex < 0) return;
        tabStops[fromIndex + (event.shiftKey ? -1 : 1)]?.focus();
      });
      return;
    }
    if ([ARROW_LEFT, ARROW_RIGHT, ARROW_UP, ARROW_DOWN, "Enter"].includes(event.key)) {
      event.preventDefault();
      if (event.key === "Enter") {
        void this.flushPunc();
      } else if (
        event.key === ARROW_LEFT
        || event.key === ARROW_RIGHT
        || event.key === ARROW_UP
        || event.key === ARROW_DOWN
      ) {
        void this.navigatePunc(event.key);
      }
    }
  }

  private async navigatePunc(
    direction: "ArrowLeft" | "ArrowRight" | "ArrowUp" | "ArrowDown",
  ): Promise<void> {
    const session = this.punc;
    if (session === null) return;
    const previousParagraphId = session.b.paragraphId;
    const target = this.punctuationNavigationTarget(direction, session);
    if (!await this.flushPunc()) return;
    // Closing an unchanged session no longer rebuilds the paragraph, so a
    // caret rendered through the session preview would stay mounted on the
    // source paragraph; refresh it before starting the session elsewhere.
    if (target !== null && target.paragraphId !== previousParagraphId) {
      this.view.setCaret(null);
      this.patchParagraphs(DRAFT_SURFACE, [previousParagraphId]);
    }
    if (target === null) return;
    const snapshot = this.requireSnapshot();
    const paragraph = snapshot.paragraphs.find(
      (candidate) => candidate.paragraph_id === target.paragraphId,
    );
    if (paragraph?.kind !== SOURCE_EXCERPT_KIND) return;
    const offset = Math.min(target.characterOffset, Array.from(paragraph.text).length);
    const point: DraftEditorRequestPoint = {
      paragraph_id: paragraph.paragraph_id,
      offset: codePointToUtf16(paragraph.text, offset),
      offset_encoding: "utf16",
    };
    this.startPunc(point);
  }

  private punctuationNavigationTarget(
    direction: "ArrowLeft" | "ArrowRight" | "ArrowUp" | "ArrowDown",
    session: PuncSession,
  ): { paragraphId: string; characterOffset: number } | null {
    const snapshot = this.requireSnapshot();
    const paragraph = snapshot.paragraphs.find(
      (candidate) => candidate.paragraph_id === session.b.paragraphId,
    );
    if (paragraph?.kind !== SOURCE_EXCERPT_KIND) return null;
    let currentOffset: number;
    try {
      currentOffset = utf16ToCodePoint(session.value, this.pStart);
    } catch {
      return null;
    }
    if (direction === ARROW_LEFT || direction === ARROW_RIGHT) {
      const selectionStart = utf16ToCodePoint(session.value, this.pStart);
      const selectionEnd = utf16ToCodePoint(session.value, this.pEnd);
      if (direction === ARROW_LEFT) {
        return {
          paragraphId: paragraph.paragraph_id,
          characterOffset: this.pStart !== this.pEnd
            ? Math.min(selectionStart, selectionEnd)
            : Math.max(0, currentOffset - 1),
        };
      }
      return {
        paragraphId: paragraph.paragraph_id,
        characterOffset: this.pStart !== this.pEnd
          ? Math.max(selectionStart, selectionEnd)
          : Math.min(Array.from(session.value).length, currentOffset + 1),
      };
    }
    const currentIndex = snapshot.paragraphs.findIndex(
      (candidate) => candidate.paragraph_id === paragraph.paragraph_id,
    );
    const step = direction === ARROW_UP ? -1 : 1;
    for (
      let index = currentIndex + step;
      index >= 0 && index < snapshot.paragraphs.length;
      index += step
    ) {
      const candidate = snapshot.paragraphs[index];
      if (candidate?.kind !== SOURCE_EXCERPT_KIND) continue;
      return {
        paragraphId: candidate.paragraph_id,
        characterOffset: Math.min(currentOffset, Array.from(candidate.text).length),
      };
    }
    return null;
  }

  private tabbableElements(): HTMLElement[] {
    return Array.from(
      document.querySelectorAll<HTMLElement>(
        "button, [href], input, select, textarea, [tabindex]",
      ),
    ).filter((candidate) => (
      !candidate.hasAttribute("disabled")
      && candidate.tabIndex >= 0
      && !candidate.hidden
      && candidate.getClientRects().length > 0
    ));
  }

  private applyPunc(
    replacement: string,
    mode: PunctuationInputMode,
  ): void {
    const session = this.punc;
    if (session === null) return;
    const start = this.pStart;
    const end = this.pEnd;
    const result = session.apply(start, end, replacement, mode);
    if (!result.accepted) {
      if (result.message !== undefined) this.onStatus(result.message, true);
      return;
    }
    this.acceptPunc(result, start, replacement);
  }

  private acceptPunc(
    result: { accepted: boolean; value: string },
    cursor: number,
    replacement: string,
  ): void {
    const session = this.punc;
    if (session === null) return;
    const next = codePointToUtf16(
      result.value,
      utf16ToCodePoint(result.value, cursor) + Array.from(replacement).length,
    );
    this.pStart = next;
    this.pEnd = next;
    this.pPreview = { paragraphId: session.b.paragraphId, text: result.value };
    if (this.pInput !== null) this.pInput.value = "";
    this.patchParagraphs(DRAFT_SURFACE, [session.b.paragraphId]);
  }

  private deletePunc(forward: boolean): void {
    const session = this.punc;
    if (session === null) return;
    const text = session.value;
    if (this.pStart !== this.pEnd) {
      this.applyPunc("", "typing");
      return;
    }
    let start = this.pStart;
    let end = this.pEnd;
    const current = utf16ToCodePoint(text, this.pStart);
    const characters = Array.from(text);
    if (forward) {
      if (current >= characters.length) return;
      end = codePointToUtf16(text, current + 1);
    } else {
      if (current <= 0) return;
      start = codePointToUtf16(text, current - 1);
    }
    const target = characters[forward ? current : current - 1] ?? "";
    if (!/^\p{P}$/u.test(target)) {
      this.onStatus(PUNCTUATION_NON_PUNCTUATION_MESSAGE, true);
      return;
    }
    const result = session.apply(start, end, "", "typing");
    if (!result.accepted) {
      if (result.message !== undefined) this.onStatus(result.message, true);
      return;
    }
    this.acceptPunc(result, start, "");
  }

  private cancelPunc(): void {
    const session = this.punc;
    if (session === null) return;
    const paragraphId = session.b.paragraphId;
    session.cancel();
    this.invalidatePuncSession();
    this.patchParagraphs(DRAFT_SURFACE, [paragraphId]);
    this.renderInteractionState();
  }

  private async flushPunc(): Promise<boolean> {
    if (this.pFlush !== null) {
      return this.pFlush;
    }
    const session = this.punc;
    if (session === null) return true;
    if (!session.changed) {
      this.closePuncSession(session);
      return true;
    }
    const payload = session.payload();
    if (payload === null) {
      this.closePuncSession(session);
      return true;
    }
    const basis: DraftWorkspaceMutationBasis = {
      expected_checkpoint_ref: { ...session.b.checkpoint },
      expected_current_candidate_ref: { ...session.b.currentCandidate },
    };
    this.pFlush = (async () => {
      this.busy = true;
      this.renderInteractionState();
      try {
        const response = await this.api<EditResponse>(
          DRAFT_EDIT_API,
          post(this.retryableWorkspaceMutation.mutation(
            "draft-edit:punctuation_edit",
            {
              schema_version: 2,
              operation: "punctuation_edit",
              payload,
            },
            basis,
          )),
        );
        if (this.punc !== session) return false;
        this.invalidatePuncSession();
        this.applySnapshot(response.draft_editor);
        this.onStatus("标点已保存为新的初稿版本。");
        return true;
      } catch (error) {
        this.handleError(error);
        return false;
      } finally {
        this.busy = false;
        this.renderInteractionState();
      }
    })();
    try {
      return await this.pFlush;
    } finally {
      this.pFlush = null;
    }
  }

  private closePuncSession(session: PuncSession): void {
    if (this.punc !== session) return;
    if (session.changed) {
      const paragraphId = session.b.paragraphId;
      this.patchParagraphs(DRAFT_SURFACE, [paragraphId]);
    }
    this.invalidatePuncSession();
    this.renderInteractionState();
  }

  private async handleDocumentClick(
    surface: DraftEditorSurface,
    event: MouseEvent,
  ): Promise<void> {
    if (this.pointerGesture.consumesClick(surface)) return;
    if (surface !== DRAFT_SURFACE || this.view.locked) return;
    const selection = window.getSelection();
    if (selection !== null && !selection.isCollapsed) return;
    if (!await this.flushPunc() || this.busy) return;
    const point = endpointFromPoint(event.clientX, event.clientY, DRAFT_SURFACE);
    if (point === null) return;
    this.clearDraftSelection();
    this.startPunc(point);
    await this.resolveCaret(point);
  }

  private clearDraftSelection(): void {
    const affected = new Set([
      ...this.selectionParagraphIds(DRAFT_SURFACE),
      ...this.selectionParagraphIds(SOURCE_SURFACE),
      ...this.correspondenceParagraphIds(),
    ]);
    this.view.clearSelections();
    this.correspondences = [];
    this.correspondenceIndex = 0;
    this.playback.reset();
    this.patchParagraphs(DRAFT_SURFACE, affected);
    this.patchParagraphs(SOURCE_SURFACE, affected);
    this.renderCorrespondenceNavigation();
    this.renderPlayer();
    this.renderInteractionState();
  }

  private async revealCorrespondence(
    index: number,
    selectionToken?: DraftSelectionToken,
  ): Promise<void> {
    if (
      selectionToken !== undefined
      && !this.selectionIsCurrent(selectionToken, selectionToken.candidateId)
    ) return;
    const correspondence = this.correspondences[index];
    if (correspondence === undefined) return;
    const previous = this.correspondenceParagraphIds();
    this.correspondenceIndex = index;
    this.view.setSourceLocation({
      sourceId: correspondence.sourceId,
      paragraphId: correspondence.paragraphId,
    });
    element<HTMLSelectElement>(DRAFT_SOURCE_SELECT_ID).value = correspondence.sourceId;
    await this.loadTranscriptWindow(
      this.transcriptWindow.locate(
        correspondence.sourceId,
        correspondence.paragraphId,
      ),
      true,
    );
    if (
      selectionToken !== undefined
      && !this.selectionIsCurrent(selectionToken, selectionToken.candidateId)
    ) return;
    this.patchParagraphs(SOURCE_SURFACE, previous);
    this.patchParagraphs(SOURCE_SURFACE, [correspondence.paragraphId]);
    this.renderCorrespondenceNavigation();
  }

  private async stepCorrespondence(direction: -1 | 1): Promise<void> {
    if (this.correspondences.length < 2) return;
    const index = (
      this.correspondenceIndex + direction + this.correspondences.length
    ) % this.correspondences.length;
    await this.revealCorrespondence(index);
  }

  private renderCorrespondenceNavigation(): void {
    const navigation = element<HTMLElement>("draft-correspondence-nav");
    navigation.hidden = this.correspondences.length === 0;
    if (this.correspondences.length === 0) return;
    const label = element<HTMLElement>("draft-correspondence-label");
    label.textContent = this.correspondences.length === 1
      ? "对应原稿"
      : `对应 ${this.correspondences.length} 处原稿 · ${this.correspondenceIndex + 1}/${this.correspondences.length}`;
    element<HTMLButtonElement>("draft-correspondence-prev").disabled =
      this.correspondences.length < 2;
    element<HTMLButtonElement>("draft-correspondence-next").disabled =
      this.correspondences.length < 2;
  }

  private async captureSelection(surface: DraftEditorSurface): Promise<void> {
    if (this.busy) return;
    const selection = window.getSelection();
    if (selection === null || selection.isCollapsed) return;
    const anchor = endpointFromDom(selection.anchorNode, selection.anchorOffset, surface)
      ?? narrationEndpointFromDom(selection.anchorNode, selection.anchorOffset, surface);
    const focus = endpointFromDom(selection.focusNode, selection.focusOffset, surface)
      ?? narrationEndpointFromDom(selection.focusNode, selection.focusOffset, surface);
    if (anchor === null || focus === null) {
      this.rejectUnresolvedSelection();
      return;
    }
    const request = { anchor, focus };
    this.invalidateCaretResolve();
    const startedAt = performance.now();
    this.editPerformanceTrace.reset();
    const candidateId = this.requireSnapshot().candidate.candidate_id;
    const token = this.selectionGeneration.begin(candidateId);
    const previousDraftSelection = this.selectionParagraphIds(DRAFT_SURFACE);
    const previousSourceSelection = this.selectionParagraphIds(SOURCE_SURFACE);
    const previousCorrespondence = this.correspondenceParagraphIds();
    this.transcriptRequestGeneration += 1;
    this.degradedPromptOperation = null;
    element<HTMLElement>(DRAFT_SOURCE_DOCUMENT_ID).removeAttribute(ARIA_BUSY);
    this.view.clearSelections();
    this.correspondences = [];
    this.correspondenceIndex = 0;
    this.playback.reset();
    this.selectionPending = true;
    this.resetSelectionTiming();
    this.patchParagraphs(
      surface === DRAFT_SURFACE ? SOURCE_SURFACE : DRAFT_SURFACE,
      surface === DRAFT_SURFACE ? previousSourceSelection : previousDraftSelection,
    );
    this.patchParagraphs(SOURCE_SURFACE, previousCorrespondence);
    this.renderCorrespondenceNavigation();
    this.renderPlayer();
    this.renderInteractionState();
    this.onStatus("正在对齐可剪边界…");
    try {
      const response = await this.api<DraftEditorSelectionResponse>(
        "/api/workflow/draft-selection-resolve",
        post({
          candidate_id: candidateId,
          surface,
          anchor: request.anchor,
          focus: request.focus,
        }),
      );
      if (!this.selectionIsCurrent(token, response.candidate_id)) return;
      this.editPerformanceTrace.record(
        SELECTION_RESOLVE_PHASE,
        performance.now() - startedAt,
      );
      this.recordSelectionTiming("selectionResolveMs", startedAt);
      this.view.focusedSurface = surface;
      this.view.setSelection(surface, {
        surface,
        request,
        response,
        acceptedDegraded: !response.resolution.degraded,
      });
      window.getSelection()?.removeAllRanges();
      this.preparePlayback(response.resolution.refs);
      this.patchParagraphs(DRAFT_SURFACE, previousDraftSelection);
      this.patchParagraphs(SOURCE_SURFACE, previousSourceSelection);
      this.patchParagraphs(surface, this.selectionParagraphIds(surface));
      if (surface === DRAFT_SURFACE) {
        this.correspondences = draftCorrespondences(response);
        this.correspondenceIndex = 0;
      }
      this.renderCorrespondenceNavigation();
      this.renderInteractionState();
      this.selectionTimingStartedAt = startedAt;
      this.recordSelectionTiming("persistentHighlightMs", startedAt);
      this.onStatus(
        "已对齐可剪边界，可继续删除、移动、加入或试听。",
      );
      window.requestAnimationFrame(() => {
        void this.finishSelectionEffects(surface, token, startedAt);
      });
    } catch (error) {
      if (this.selectionIsCurrent(token, candidateId)) {
        if (
          (error as { code?: unknown }).code === INVALID_WORKFLOW_CHANGE
          && typeof (error as { message?: unknown }).message === "string"
          && (error as { message: string }).message.includes("cannot cross a person")
        ) {
          // The dragged range spans more than one speaker; the server
          // rejects it and the friendly unrecognized-selection status is
          // the right UX.
          this.rejectUnresolvedSelection();
        } else {
          this.handleError(error);
        }
      }
    } finally {
      if (this.selectionIsCurrent(token, candidateId)) {
        this.selectionPending = false;
        this.renderToolbar();
      }
    }
  }

  private rejectUnresolvedSelection(): void {
    const previousDraft = this.selectionParagraphIds(DRAFT_SURFACE);
    const previousSource = this.selectionParagraphIds(SOURCE_SURFACE);
    const previousCorrespondence = this.correspondenceParagraphIds();
    this.invalidateCaretResolve();
    this.selectionGeneration.invalidate();
    this.transcriptRequestGeneration += 1;
    this.degradedPromptOperation = null;
    this.selectionPending = false;
    this.view.clearSelections();
    this.correspondences = [];
    this.correspondenceIndex = 0;
    this.playback.reset();
    window.getSelection()?.removeAllRanges();
    this.patchParagraphs(DRAFT_SURFACE, previousDraft);
    this.patchParagraphs(SOURCE_SURFACE, [...previousSource, ...previousCorrespondence]);
    this.renderCorrespondenceNavigation();
    this.renderPlayer();
    this.renderInteractionState();
    this.onStatus("无法识别本次文字选区，请在正文文字内重新选择。", true);
  }

  private async finishSelectionEffects(
    surface: DraftEditorSurface,
    token: DraftSelectionToken,
    startedAt: number,
  ): Promise<void> {
    if (!this.selectionIsCurrent(token, token.candidateId)) return;
    if (surface === DRAFT_SURFACE && this.correspondences.length > 0) {
      await this.revealCorrespondence(0, token);
      if (!this.selectionIsCurrent(token, token.candidateId)) return;
    }
    this.recordSelectionTiming("correspondenceWindowMs", startedAt);
    if (this.playerExpanded) this.activateCurrentPlayback(false);
  }

  private async resolveCaret(point: DraftEditorRequestPoint): Promise<void> {
    const previous = this.view.caret?.paragraph_id;
    const candidateId = this.requireSnapshot().candidate.candidate_id;
    const token = this.caretGeneration.begin(candidateId);
    try {
      const caret = await this.api<DraftEditorCaret>(
        "/api/workflow/draft-caret-resolve",
        post({
          candidate_id: candidateId,
          paragraph_id: point.paragraph_id,
          offset: point.offset,
          offset_encoding: "utf16",
        }),
      );
      if (!this.caretResolveIsCurrent(token, candidateId)) {
        return;
      }
      if (caret.candidate_id !== candidateId) {
        this.view.setCaret(null);
        this.patchParagraphs(DRAFT_SURFACE, previous === undefined ? [] : [previous]);
        this.renderInteractionState();
        this.onStatus("初稿版本已变化，已清除旧的插入光标。", true);
        return;
      }
      if (this.punc?.changed) return;
      if (caret.degraded) {
        this.view.setCaret(null);
        this.patchParagraphs(DRAFT_SURFACE, previous === undefined ? [] : [previous]);
        this.renderInteractionState();
        this.onStatus("该位置缺少可确认的精确边界，未设置插入光标。", true);
        return;
      }
      this.view.setCaret(caret);
      this.patchParagraphs(DRAFT_SURFACE, [
        ...(previous === undefined ? [] : [previous]),
        caret.paragraph_id,
      ]);
      this.renderInteractionState();
    } catch (error) {
      if (!this.caretResolveIsCurrent(token, candidateId)) {
        return;
      }
      this.handleError(error);
    } finally {
      if (this.caretResolveIsCurrent(token, candidateId)) {
        this.renderInteractionState();
      }
    }
  }

  private preparePlayback(refs: ResolvedSelectionRef[]): void {
    this.playback.replace(refs);
    this.renderPlayer();
  }

  private togglePlayer(): void {
    this.playerExpanded = !this.playerExpanded;
    this.renderPlayer();
    if (this.playerExpanded) {
      this.activateCurrentPlayback(false);
      return;
    }
    const player = element<HTMLVideoElement>(DRAFT_PLAYER_ID);
    player.pause();
    player.removeAttribute("src");
    player.load();
  }

  private renderPlayer(): void {
    const panel = document.querySelector<HTMLElement>(".draft-player");
    if (panel === null) return;
    panel.dataset.expanded = String(this.playerExpanded);
    const expanded = element<HTMLElement>("draft-player-expanded");
    expanded.hidden = !this.playerExpanded;
    const toggle = element<HTMLButtonElement>("draft-player-toggle");
    toggle.setAttribute("aria-expanded", String(this.playerExpanded));
    toggle.textContent = this.playerExpanded ? "收起播放器" : "展开播放器";
    const segments = this.playback.all;
    const sourceCount = new Set(segments.map((segment) => segment.sourceId)).size;
    element<HTMLElement>(DRAFT_PLAYER_CAPTION_ID).textContent = segments.length === 0
      ? "选择有时码的文字后可播放"
      : `${segments.length} 段 · ${sourceCount} 个素材 · 按初稿顺序试听`;
  }

  private activateCurrentPlayback(autoplay: boolean): void {
    const segment = this.playback.current;
    if (!this.playerExpanded || segment === null) return;
    const source = this.requireSnapshot().sources.find(
      (item) => item.source_id === segment.sourceId,
    );
    if (source === undefined) return;
    const player = element<HTMLVideoElement>(DRAFT_PLAYER_ID);
    const seekAndMaybePlay = (): void => {
      if (this.selectionTimingStartedAt !== null) {
        this.recordSelectionTiming("mediaReadyMs", this.selectionTimingStartedAt);
      }
      player.currentTime = ticksToSeconds(segment.startTicks);
      if (autoplay) void player.play();
    };
    if (player.getAttribute("src") !== source.media_url) {
      player.src = source.media_url;
      player.addEventListener(LOADED_METADATA_EVENT, seekAndMaybePlay, { once: true });
    } else if (player.readyState >= HTMLMediaElement.HAVE_METADATA) {
      seekAndMaybePlay();
    } else {
      player.addEventListener(LOADED_METADATA_EVENT, seekAndMaybePlay, { once: true });
    }
    element<HTMLElement>(DRAFT_PLAYER_CAPTION_ID).textContent =
      `${this.playback.currentIndex + 1}/${this.playback.all.length} · ${source.display_name} · ${formatDuration(segment.startTicks)}–${formatDuration(segment.endTicks)}`;
    element<HTMLElement>("draft-media-error").hidden = true;
  }

  private async advancePlayback(): Promise<void> {
    const player = element<HTMLVideoElement>(DRAFT_PLAYER_ID);
    player.pause();
    const next = this.playback.advance();
    if (next === null) {
      element<HTMLElement>(DRAFT_PLAYER_CAPTION_ID).textContent = "所选内容已试听完成";
      return;
    }
    this.activateCurrentPlayback(true);
  }

  private async applyEdit(operation: "delete"): Promise<void> {
    if (this.view.locked || !await this.flushPunc() || this.busy) return;
    const selection = this.view.draftSelection;
    if (selection === null) return;
    if (selection.response.resolution.degraded && !selection.acceptedDegraded) {
      this.degradedPromptOperation = operation;
      this.renderDegradedPrompt();
      return;
    }
    const sourceDecorations = new Set([
      ...this.selectionParagraphIds(SOURCE_SURFACE),
      ...this.correspondenceParagraphIds(),
    ]);
    const operationStartedAt = performance.now();
    this.resetEditTiming();
    this.busy = true;
    this.renderToolbar();
    this.onStatus("正在保存新的初稿版本…");
    try {
      this.recordEditTiming("editSavingFeedbackMs", operationStartedAt);
      const requestStartedAt = performance.now();
      const response = await this.api<EditResponse>(
        DRAFT_EDIT_API,
        post(this.workspaceMutation(`draft-edit:${operation}`, {
          candidate_id: this.requireSnapshot().candidate.candidate_id,
          operation,
          selection: {
            surface: selection.surface,
            anchor: selection.request.anchor,
            focus: selection.request.focus,
          },
          accept_degraded: selection.acceptedDegraded,
        })),
      );
      this.recordEditTiming("editBrowserRequestResponseMs", requestStartedAt);
      this.editPerformanceTrace.record(
        DROP_HTTP_SERVER_PHASE,
        performance.now() - requestStartedAt,
      );
      if (response.timing !== undefined) {
        Object.assign(
          element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset,
          serverDraftEditTimingDataset(operation, response.timing),
        );
      }
      const domPatchStartedAt = performance.now();
      this.view.clearSelections();
      this.view.setCaret(null);
      this.correspondences = [];
      this.correspondenceIndex = 0;
      this.playback.reset();
      this.applySnapshot(response.draft_editor);
      this.patchParagraphs(SOURCE_SURFACE, sourceDecorations);
      this.renderCorrespondenceNavigation();
      this.renderPlayer();
      this.recordEditTiming("editDomPatchMs", domPatchStartedAt);
      this.editPerformanceTrace.record(
        DOM_PATCH_PHASE,
        performance.now() - domPatchStartedAt,
      );
      this.publishEditPerformance(operation);
      this.onStatus("已删除所选内容，并保存为新的初稿版本。");
      this.recordEditTiming("editCompletionVisibleMs", operationStartedAt);
      const searchStartedAt = performance.now();
      void this.refreshSearches().then(() => {
        this.recordEditTiming("editSearchRefreshMs", searchStartedAt);
      });
    } catch (error) {
      this.handleError(error);
    } finally {
      this.busy = false;
      this.renderInteractionState();
    }
  }

  private async saveNarration(
    paragraph: DraftEditorParagraph,
    text: string,
  ): Promise<void> {
    if (
      paragraph.block_id === undefined
      || this.view.locked
      || !await this.flushPunc()
      || this.busy
    ) return;
    if (text === paragraph.text) {
      this.narrationEditorStates.delete(paragraph.paragraph_id);
      this.replaceNarrationParagraph(paragraph);
      return;
    }
    this.invalidateCaretResolve();
    this.busy = true;
    this.renderInteractionState();
    this.onStatus("正在保存解说文字…");
    try {
      const response = await this.api<NarrationResponse>(
        "/api/workflow/draft-narration",
        post(this.workspaceMutation("draft-narration", {
          candidate_id: this.requireSnapshot().candidate.candidate_id,
          block_id: paragraph.block_id,
          text,
        })),
      );
      this.view.clearSelections();
      this.view.setCaret(null);
      this.correspondences = [];
      this.correspondenceIndex = 0;
      this.playback.reset();
      this.applySnapshot(response.draft_editor);
      this.renderCorrespondenceNavigation();
      this.renderPlayer();
      await this.refreshSearches();
      this.onStatus(
        paragraph.narration_status === "recorded"
          ? "解说已保存为新的不可变 child；旧录音绑定已清除，状态回到待录音。重新录音、校对并绑定前不能生成粗剪。"
          : "解说文字已保存为新的初稿版本；录音状态为待录音。",
      );
    } catch (error) {
      this.handleError(error);
    } finally {
      this.busy = false;
      this.renderInteractionState();
    }
  }

  private async navigateHistory(redo: boolean): Promise<void> {
    if (this.view.locked || !await this.flushPunc() || this.busy) return;
    this.invalidateCaretResolve();
    this.busy = true;
    this.renderInteractionState();
    this.onStatus(redo ? "正在重做上一项调整…" : "正在撤销上一项调整…");
    try {
      const response = await this.api<EditResponse>(
        redo ? "/api/workflow/draft-redo" : "/api/workflow/draft-undo",
        post(this.workspaceMutation(redo ? DRAFT_REDO_ID : DRAFT_UNDO_ID, {
          candidate_id: this.requireSnapshot().candidate.candidate_id,
        })),
      );
      this.view.clearSelections();
      this.view.setCaret(null);
      this.correspondences = [];
      this.correspondenceIndex = 0;
      this.playback.reset();
      this.applySnapshot(response.draft_editor);
      this.renderSections();
      this.renderCorrespondenceNavigation();
      this.renderPlayer();
      await this.refreshSearches();
      this.onStatus(redo ? "已重做上一项初稿调整。" : "已撤销上一项初稿调整。");
    } catch (error) {
      this.handleError(error);
    } finally {
      this.busy = false;
      this.renderInteractionState();
    }
  }

  private async confirmDraft(): Promise<void> {
    if (this.view.locked || !await this.flushPunc() || this.busy) return;
    this.invalidateCaretResolve();
    const initial = this.requireSnapshot();
    if (initial.candidate.has_unrecorded_narration) {
      this.onStatus("还有解说尚未录音，完成录音并加入真实素材后才能生成粗剪预览。", true);
      return;
    }
    this.busy = true;
    this.renderInteractionState();
    let confirmedContentDraftId = initial.candidate.confirmed_by_user
      ? initial.candidate.candidate_id
      : null;
    try {
      this.onStatus(
        initial.candidate.confirmed_by_user
          ? "正在生成粗剪预览…"
          : "正在确认初稿…",
      );
      await performWorkflowProposalHandoff(
        this.api,
        initial.candidate.candidate_id,
        initial.candidate.confirmed_by_user,
        this.workspaceMutation("approve-draft", {
          content_draft_id: initial.candidate.candidate_id,
        }),
        async (confirmedId) => {
          confirmedContentDraftId = confirmedId;
          this.onStatus("正在生成粗剪预览…");
        },
        this.onProposalHandoff,
      );
      this.proposalRetryRequired = false;
    } catch (error) {
      if (confirmedContentDraftId === null) {
        try {
          const recovered = await this.api<DraftEditorSnapshot>(
            DRAFT_EDITOR_API,
          );
          if (
            recovered.candidate.confirmed_by_user
            && recovered.candidate.parent_candidate_id
              === initial.candidate.candidate_id
          ) {
            confirmedContentDraftId = recovered.candidate.candidate_id;
            this.applySnapshot(recovered);
          }
        } catch {
          // The original confirmation error remains authoritative.
        }
      }
      if (confirmedContentDraftId !== null) {
        this.proposalRetryRequired = true;
        this.onStatus(
          "初稿已确认，粗剪预览生成失败。请点击“重新生成粗剪预览”再试；不会重复确认初稿。",
          true,
        );
      } else {
        this.handleError(error);
      }
    } finally {
      this.busy = false;
      this.renderInteractionState();
    }
  }

  private renderToolbar(): void {
    const draftSelection = this.view.draftSelection;
    const sourceSelection = this.view.sourceSelection;
    const draftAccepted = draftSelection !== null
      && draftSelection.response.resolution.refs.length > 0
      && (
        !draftSelection.response.resolution.degraded
        || draftSelection.acceptedDegraded
      );
    const locked = this.view.locked || this.busy || this.selectionPending;
    const otherWritesLocked = locked;
    element<HTMLButtonElement>("draft-delete").disabled =
      otherWritesLocked || !draftAccepted;
    element<HTMLButtonElement>("draft-section-split").disabled =
      otherWritesLocked || this.view.draftMode !== "body" || this.sectionHeadingForCaret() === null;
    const history = this.requireSnapshot().history;
    element<HTMLButtonElement>(DRAFT_UNDO_ID).disabled =
      otherWritesLocked || !history.can_undo;
    element<HTMLButtonElement>(DRAFT_REDO_ID).disabled =
      otherWritesLocked || !history.can_redo;
    const help = element<HTMLElement>("draft-toolbar-help");
    help.textContent = this.selectionPending
      ? "正在对齐可剪边界…"
      : this.view.lastSelectionSurface === DRAFT_SURFACE && draftSelection !== null
      && draftSelection.response.resolution.degraded
      && !draftSelection.acceptedDegraded
      ? "初稿内容已选中；删除或移动时会说明可用时码范围。"
      : this.view.lastSelectionSurface === DRAFT_SURFACE && draftSelection !== null
      ? this.view.caret === null
        ? "初稿内容已选中，可删除；从高亮区域开始拖动可移动。"
        : "初稿内容已选中，可删除或从高亮区域开始拖动。"
      : sourceSelection !== null
        ? this.view.caret === null
          ? "原稿内容已选中；拖入初稿中的合法插入线。"
          : "原稿内容已选中，可拖入蓝色插入线。"
        : this.view.caret === null
          ? "在初稿中点击文字位置，放置插入光标。"
          : "插入位置已设置；继续选择要移动或加入的内容。";
  }

  private renderDegradedPrompt(): void {
    const prompt = element<HTMLElement>("draft-degraded");
    const selection = this.view.selectionFor(this.view.lastSelectionSurface);
    const visible = this.degradedPromptOperation !== null
      && selection !== null
      && selection.response.resolution.degraded
      && !selection.acceptedDegraded;
    prompt.hidden = !visible;
    if (visible) {
      element<HTMLElement>("draft-degraded-copy").textContent =
        `为避免猜测时码，${degradedOperationLabel(this.degradedPromptOperation)}将使用完整原话：“${selection.response.resolution.canonical_text}”`;
    }
  }

  private renderConfirm(): void {
    const snapshot = this.requireSnapshot();
    const button = element<HTMLButtonElement>("draft-confirm");
    const confirmed = snapshot.candidate.confirmed_by_user;
    const unrecorded = snapshot.candidate.has_unrecorded_narration;
    button.disabled = this.view.locked || this.busy || unrecorded;
    button.textContent = this.proposalRetryRequired
      ? "重新生成粗剪预览"
      : confirmed
        ? "生成粗剪预览"
        : "确认初稿并生成粗剪预览";
    const heading = element<HTMLElement>("draft-confirm-heading");
    const copy = element<HTMLElement>("draft-confirm-copy");
    heading.textContent = confirmed ? "初稿已确认" : "确认初稿并生成粗剪预览";
    copy.textContent = unrecorded
      ? "还有解说尚未录音，生成粗剪预览前需要先加入真实录音素材。"
      : confirmed
        ? "将从这版已确认初稿生成待审阅粗剪；不会自动采用或渲染。"
        : "确认后生成待审阅粗剪；不会自动采用、正式渲染或导出。";
    const stale = element<HTMLElement>("draft-stale");
    stale.hidden = !this.view.locked;
    if (this.lockReason !== null) {
      for (const child of stale.children as HTMLCollectionOf<HTMLElement>) {
        child.textContent = child.dataset[this.lockReason]!;
      }
    }
  }

  private openSearch(surface: DraftEditorSurface): void {
    this.view.focusedSurface = surface;
    this.search[surface].open = true;
    this.renderSearch(surface);
    const panel = findPanel(surface);
    panel.querySelector<HTMLInputElement>(INPUT_ELEMENT)?.focus();
  }

  private closeSearch(surface: DraftEditorSurface): void {
    const state = this.search[surface];
    const previousMatches = new Set(state.matches.map((match) => match.paragraph_id));
    if (state.timer !== null) window.clearTimeout(state.timer);
    state.open = false;
    state.query = "";
    state.matches = [];
    state.current = 0;
    this.view.setSearch(surface, "");
    this.patchParagraphs(surface, previousMatches);
    this.renderSearch(surface, true);
  }

  private scheduleSearch(surface: DraftEditorSurface, query: string): void {
    const state = this.search[surface];
    if (state.timer !== null) window.clearTimeout(state.timer);
    state.query = query;
    state.timer = window.setTimeout(() => void this.runSearch(surface), 180);
  }

  private async runSearch(
    surface: DraftEditorSurface,
    candidateId = this.requireSnapshot().candidate.candidate_id,
  ): Promise<void> {
    const state = this.search[surface];
    const query = state.query.trim();
    state.timer = null;
    if (!query) {
      const previousMatches = new Set(state.matches.map((match) => match.paragraph_id));
      state.matches = [];
      state.current = 0;
      this.view.setSearch(surface, "");
      this.patchParagraphs(surface, previousMatches);
      this.renderSearch(surface, true);
      return;
    }
    try {
      const previousMatches = new Set(state.matches.map((match) => match.paragraph_id));
      const matches: DraftEditorSearchMatch[] = [];
      let offset = 0;
      while (true) {
        const page = await this.api<DraftEditorSearchPage>(
          "/api/workflow/draft-search",
          post({
            candidate_id: candidateId,
            surface,
            query,
            offset,
            limit: SEARCH_PAGE_SIZE,
          }),
        );
        if (
          this.requireSnapshot().candidate.candidate_id !== candidateId
          || !state.open
          || state.query.trim() !== query
        ) {
          return;
        }
        matches.push(...page.matches);
        if (page.next_cursor === null) break;
        offset = page.next_cursor;
      }
      state.matches = matches;
      state.current = Math.min(state.current, Math.max(0, matches.length - 1));
      this.view.setSearch(surface, query, state.current);
      this.patchParagraphs(surface, previousMatches);
      this.patchParagraphs(surface, matches.map((match) => match.paragraph_id));
      this.renderSearch(surface, true);
      await this.revealCurrentSearch(surface);
    } catch (error) {
      if (
        this.requireSnapshot().candidate.candidate_id === candidateId
        && state.open
        && state.query.trim() === query
      ) {
        this.handleError(error);
      }
    }
  }

  private async stepSearch(
    surface: DraftEditorSurface,
    direction: -1 | 1,
  ): Promise<void> {
    const state = this.search[surface];
    if (state.matches.length === 0) return;
    const previous = state.matches[state.current]?.paragraph_id;
    state.current = (
      state.current + direction + state.matches.length
    ) % state.matches.length;
    this.view.setSearch(surface, state.query, state.current);
    const current = state.matches[state.current]?.paragraph_id;
    this.patchParagraphs(surface, [
      ...(previous === undefined ? [] : [previous]),
      ...(current === undefined ? [] : [current]),
    ]);
    this.renderSearchProgress(surface);
    await this.revealCurrentSearch(surface);
  }

  private async revealCurrentSearch(surface: DraftEditorSurface): Promise<void> {
    const match = this.search[surface].matches[this.search[surface].current];
    if (match === undefined) return;
    if (surface === SOURCE_SURFACE && match.source_id !== null) {
      this.view.selectSource(match.source_id);
      element<HTMLSelectElement>(DRAFT_SOURCE_SELECT_ID).value = match.source_id;
      await this.loadTranscriptWindow(
        this.transcriptWindow.locate(match.source_id, match.paragraph_id),
        true,
      );
      return;
    }
    this.scrollMountedParagraph(surface, match.paragraph_id);
  }

  private renderSearch(surface: DraftEditorSurface, rebuildList = false): void {
    const state = this.search[surface];
    const panel = findPanel(surface);
    panel.hidden = !state.open;
    if (!state.open) {
      if (rebuildList) {
        panel.querySelector<HTMLOListElement>("[data-draft-find-results]")
          ?.replaceChildren();
      }
      return;
    }
    const input = panel.querySelector<HTMLInputElement>(INPUT_ELEMENT);
    if (input !== null && input.value !== state.query) input.value = state.query;
    const output = panel.querySelector<HTMLOutputElement>("output");
    if (output !== null) {
      output.textContent = state.matches.length === 0
        ? "0 / 0"
        : `${state.current + 1} / ${state.matches.length}`;
    }
    const list = panel.querySelector<HTMLOListElement>("[data-draft-find-results]");
    if (list !== null && rebuildList) {
      list.replaceChildren(
        ...state.matches.map((match, index) => {
          const item = document.createElement("li");
          const button = document.createElement(BUTTON_ELEMENT);
          button.type = BUTTON_ELEMENT;
          button.dataset.searchIndex = String(index);
          button.className = index === state.current ? "is-current" : "";
          button.textContent = [
            match.source_display_name,
            match.person_name,
            match.context,
          ].filter(Boolean).join(" · ");
          button.addEventListener("click", () => {
            const previous = state.matches[state.current]?.paragraph_id;
            state.current = index;
            this.view.setSearch(surface, state.query, index);
            this.patchParagraphs(surface, [
              ...(previous === undefined ? [] : [previous]),
              match.paragraph_id,
            ]);
            this.renderSearchProgress(surface);
            void this.revealCurrentSearch(surface);
          });
          item.append(button);
          return item;
        }),
      );
    }
  }

  private renderSearchProgress(surface: DraftEditorSurface): void {
    const state = this.search[surface];
    const panel = findPanel(surface);
    const output = panel.querySelector<HTMLOutputElement>("output");
    if (output !== null) {
      output.textContent = state.matches.length === 0
        ? "0 / 0"
        : `${state.current + 1} / ${state.matches.length}`;
    }
    panel.querySelectorAll<HTMLButtonElement>("[data-search-index]").forEach((button) => {
      button.classList.toggle(
        "is-current",
        Number(button.dataset.searchIndex) === state.current,
      );
    });
  }

  private async refreshSearches(): Promise<void> {
    const surfaces = ([DRAFT_SURFACE, SOURCE_SURFACE] as const).filter(
      (surface) => this.search[surface].open && this.search[surface].query.trim(),
    );
    if (surfaces.length === 0) return;
    const candidateId = this.requireSnapshot().candidate.candidate_id;
    await Promise.all(
      surfaces.map((surface) => this.runSearch(surface, candidateId)),
    );
  }

  private renderPaneState(): void {
    document.querySelectorAll<HTMLElement>("[data-draft-panel]").forEach((panel) => {
      const key = panel.dataset.draftPanel;
      panel.classList.toggle("is-mobile-active", key === this.view.mobilePane);
    });
    document.querySelectorAll<HTMLButtonElement>("[data-draft-pane]").forEach((button) => {
      button.setAttribute(
        ARIA_SELECTED,
        String(button.dataset.draftPane === this.view.mobilePane),
      );
    });
  }

  private scrollMountedParagraph(
    surface: DraftEditorSurface,
    paragraphId: string,
  ): void {
    requestAnimationFrame(() => {
      this.scrollMountedParagraphNow(surface, paragraphId);
    });
  }

  private scrollMountedParagraphNow(
    surface: DraftEditorSurface,
    paragraphId: string,
  ): void {
    const host = element<HTMLElement>(
      surface === DRAFT_SURFACE ? DRAFT_DOCUMENT_ID : DRAFT_SOURCE_DOCUMENT_ID,
    );
    const paragraph = [...host.querySelectorAll<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR)]
      .find((candidate) => candidate.dataset.editorParagraph === paragraphId);
    paragraph?.scrollIntoView({
      block: "center",
      behavior: surface === SOURCE_SURFACE ? "auto" : "smooth",
    });
  }

  private scrollToSectionHeading(headingId: string): void {
    const snapshot = this.requireSnapshot();
    const blocks = snapshot.blocks ?? [];
    const headingIndex = blocks.findIndex((block) => block.block_id === headingId);
    if (headingIndex < 0) return;
    const nextHeadingIndex = blocks.findIndex(
      (block, index) => index > headingIndex && block.kind === SECTION_TITLE_KIND,
    );
    const contentBlockIds = new Set(
      blocks.slice(headingIndex + 1, nextHeadingIndex < 0 ? undefined : nextHeadingIndex)
        .map((block) => block.block_id),
    );
    const contentParagraph = snapshot.paragraphs.find((paragraph) => {
      if (paragraph.kind === SECTION_TITLE_KIND) return false;
      return paragraph.kind === SOURCE_EXCERPT_KIND
        ? paragraph.source_runs.some((run) => contentBlockIds.has(run.block_id ?? ""))
        : contentBlockIds.has(paragraph.block_id ?? "");
    });
    const headingParagraph = snapshot.paragraphs.find(
      (paragraph) => paragraph.kind === SECTION_TITLE_KIND && paragraph.block_id === headingId,
    );
    const targetParagraph = contentParagraph ?? headingParagraph;
    if (targetParagraph !== undefined) this.scrollMountedParagraph(DRAFT_SURFACE, targetParagraph.paragraph_id);
  }

  private async handleShortcut(event: KeyboardEvent): Promise<void> {
    const target = event.target as HTMLElement | null;
    const editing = target?.matches("input, textarea, select") ?? false;
    if (target?.classList.contains("draft-punctuation-input")) return;
    if (this.punc !== null) {
      if (event.key === "Escape") {
        event.preventDefault();
        this.cancelPunc();
        return;
      }
      if ([ARROW_LEFT, ARROW_RIGHT, ARROW_UP, ARROW_DOWN, "Tab", "Enter"].includes(event.key)) {
        event.preventDefault();
        await this.flushPunc();
        return;
      }
    }
    const command = event.metaKey || event.ctrlKey;
    if (command && event.key.toLocaleLowerCase() === "f") {
      event.preventDefault();
      this.openSearch(this.view.focusedSurface);
      return;
    }
    if (!editing && command && event.key.toLocaleLowerCase() === "z") {
      event.preventDefault();
      await this.navigateHistory(event.shiftKey);
      return;
    }
    if (!editing && event.ctrlKey && event.key.toLocaleLowerCase() === "y") {
      event.preventDefault();
      await this.navigateHistory(true);
      return;
    }
    if (
      !editing
      && (event.key === "Backspace" || event.key === "Delete")
      && this.view.draftSelection !== null
    ) {
      event.preventDefault();
      await this.applyEdit("delete");
      return;
    }
    if (event.key === "Escape") {
      if (this.pendingDraftDrop !== null) {
        event.preventDefault();
        return;
      }
      const open = ([DRAFT_SURFACE, SOURCE_SURFACE] as const).find(
        (surface) => this.search[surface].open,
      );
      if (open !== undefined) this.closeSearch(open);
      else {
        const surface = this.view.focusedSurface;
        const paragraphs = this.selectionParagraphIds(surface);
        this.view.setSelection(surface, null);
        this.patchParagraphs(surface, paragraphs);
        this.renderInteractionState();
      }
    }
  }

  private handleError(error: unknown): void {
    if (locksDraftWorkspaceWrites(error)) {
      this.invalidateCaretResolve();
      this.lock("stale");
      return;
    }
    this.onStatus(userFacingErrorMessage(error), true);
  }

  private lock(reason: DraftLockReason): void {
    this.lockReason ??= reason;
    this.view.locked = true;
    this.onStatus(
      this.lockReason === "uncertain"
        ? "拖放请求状态不明确，页面已锁定；请重新打开初稿后确认。"
        : "项目内容已在别处更新，本页已转为只读，请重新打开初稿。",
      true,
    );
    this.renderInteractionState();
  }

  private requireSnapshot(): DraftEditorSnapshot {
    if (this.snapshot === null) throw new Error("初稿尚未载入");
    return this.snapshot;
  }

  private workspaceMutation<T extends object>(
    action: DraftWorkspaceMutationAction,
    input: T,
  ): T & DraftWorkspaceMutationBasis & { operation_id: string } {
    return this.retryableWorkspaceMutation.mutation(
      action,
      input,
      this.requireSnapshot().workspace,
    );
  }

  private selectionIsCurrent(
    token: DraftSelectionToken,
    responseCandidateId: string,
  ): boolean {
    return this.selectionGeneration.isCurrent(
      token,
      this.requireSnapshot().candidate.candidate_id,
    ) && responseCandidateId === token.candidateId;
  }

  private caretResolveIsCurrent(
    token: DraftSelectionToken,
    candidateId: string,
  ): boolean {
    if (
      !this.caretGeneration.isCurrent(
        token,
        this.requireSnapshot().candidate.candidate_id,
      )
      || candidateId !== token.candidateId
    ) {
      return false;
    }
    return true;
  }

  private invalidateCaretResolve(): void {
    this.caretGeneration.invalidate();
  }

  private resetSelectionTiming(): void {
    const shell = element<HTMLElement>(DRAFT_EDITOR_SHELL_ID);
    for (const key of [
      "selectionResolveMs",
      "persistentHighlightMs",
      "correspondenceWindowMs",
      "mediaReadyMs",
    ] as const) {
      delete shell.dataset[key];
    }
  }

  private recordSelectionTiming(
    key:
      | "selectionResolveMs"
      | "persistentHighlightMs"
      | "correspondenceWindowMs"
      | "mediaReadyMs",
    startedAt: number,
  ): void {
    element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset[key] =
      String(Math.max(0, Math.round(performance.now() - startedAt)));
  }

  private resetEditTiming(): void {
    const shell = element<HTMLElement>(DRAFT_EDITOR_SHELL_ID);
    for (const key of Object.keys(shell.dataset)) {
      if (key.startsWith("edit")) delete shell.dataset[key];
    }
  }

  private resetClientEditPerformanceDataset(): void {
    const shell = element<HTMLElement>(DRAFT_EDITOR_SHELL_ID);
    for (const key of [
      "editSelectionResolveMs",
      "editPointermoveHitTestP95Ms",
      "editPointermoveHitTestSamples",
      "editDropHttpServerMs",
      "editDomPatchMs",
    ] as const) {
      delete shell.dataset[key];
    }
  }

  private recordEditTiming(key: string, startedAt: number): void {
    element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset[key] =
      String(Math.max(0, Math.round(performance.now() - startedAt)));
  }

  private publishEditPerformance(operation: DraftEditOperation): void {
    const timing = this.editPerformanceTrace.snapshot();
    if (timing.pointermove_hit_test_ms.length === 0) return;
    Object.assign(
      element<HTMLElement>(DRAFT_EDITOR_SHELL_ID).dataset,
      draftEditPerformanceDataset(operation, timing),
    );
  }

  private renderDraftControlsDisabled(): void {
    element<HTMLElement>(DRAFT_DOCUMENT_ID)
      .querySelectorAll<HTMLTextAreaElement>("textarea")
      .forEach((input) => {
        input.disabled = this.view.locked || this.busy;
      });
    element<HTMLElement>(DRAFT_DOCUMENT_ID)
      .querySelectorAll<HTMLButtonElement>(".draft-narration-card button")
      .forEach((button) => {
        button.disabled = this.view.locked || this.busy;
      });
  }
}

function post(payload: object): RequestInit {
  return { method: "POST", body: JSON.stringify(payload) };
}

function draftWorkspaceOperationId(expectedGeneration: number): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  const suffix = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"))
    .join("");
  return `dwop_${expectedGeneration}_${suffix}`;
}

function copyDraftWorkspaceBasis(
  basis: DraftWorkspaceMutationBasis,
): DraftWorkspaceMutationBasis {
  return {
    expected_checkpoint_ref: { ...basis.expected_checkpoint_ref },
    expected_current_candidate_ref: { ...basis.expected_current_candidate_ref },
  };
}

function copyDisplayRange(
  range: DraftEditorSelectionResponse["display_range"],
): DraftEditorSelectionResponse["display_range"] {
  return {
    anchor: { ...range.anchor },
    focus: { ...range.focus },
  };
}

function schema2Ref(
  ref: ResolvedSelectionRef,
): Omit<ResolvedSelectionRef, "canonical_text"> {
  return {
    source_id: ref.source_id,
    transcript_version_id: ref.transcript_version_id,
    segment_id: ref.segment_id,
    start_ticks: ref.start_ticks,
    end_ticks: ref.end_ticks,
  };
}

function narrationEndpointFromDom(
  node: Node | null,
  domOffset: number,
  surface: DraftEditorSurface,
): DraftEditorRequestPoint | null {
  if (node === null || !Number.isInteger(domOffset) || domOffset < 0) return null;
  const element = node.nodeType === 1 ? node as Element : node.parentElement;
  const paragraph = element?.closest<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR);
  if (
    paragraph === null
    || paragraph === undefined
    || paragraph.dataset.surface !== surface
    || paragraph.dataset.editorParagraph === undefined
    || paragraph.dataset.paragraphKind !== NARRATION_KIND
  ) return null;
  const copy = element?.closest<HTMLElement>(".draft-narration-copy");
  if (copy === null || copy === undefined) return null;
  let offset: number | null = null;
  if (node.nodeType === 3 && node.parentElement === copy) {
    const length = node.textContent?.length ?? 0;
    if (domOffset <= length) offset = domOffset;
  } else if (node === copy && domOffset <= copy.childNodes.length) {
    offset = [...copy.childNodes]
      .slice(0, domOffset)
      .reduce((total, child) => total + (child.textContent?.length ?? 0), 0);
  }
  if (offset === null) return null;
  return {
    paragraph_id: paragraph.dataset.editorParagraph,
    offset,
    offset_encoding: "utf16",
  };
}

function endpointFromPoint(
  x: number,
  y: number,
  surface: DraftEditorSurface,
): DraftEditorRequestPoint | null {
  const modern = document as Document & {
    caretPositionFromPoint?: (clientX: number, clientY: number) => {
      offsetNode: Node;
      offset: number;
    } | null;
  };
  const position = modern.caretPositionFromPoint?.(x, y);
  if (position !== undefined && position !== null) {
    return endpointFromDom(position.offsetNode, position.offset, surface);
  }
  const legacy = document as Document & {
    caretRangeFromPoint?: (clientX: number, clientY: number) => Range | null;
  };
  const range = legacy.caretRangeFromPoint?.(x, y);
  return range === undefined || range === null
    ? null
    : endpointFromDom(range.startContainer, range.startOffset, surface);
}

function sourceBlockAtDisplayOffset(
  runs: DraftEditorParagraph["source_runs"],
  offset: number,
): string | null {
  for (let index = 0; index < runs.length; index += 1) {
    const run = runs[index]!;
    if (run.block_id === undefined) continue;
    if (run.start_offset <= offset && offset < run.end_offset) return run.block_id;
    if (offset === run.end_offset) {
      const next = runs[index + 1];
      if (next !== undefined && next.start_offset === offset && next.block_id !== undefined) {
        return next.block_id;
      }
      return run.block_id;
    }
  }
  return null;
}

export function endpointFromRenderedFragments(
  x: number,
  y: number,
  surface: DraftEditorSurface,
): DraftEditorRequestPoint | null {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const hit = document.elementsFromPoint?.(x, y)[0]
    ?? document.elementFromPoint?.(x, y)
    ?? null;
  const paragraph = hit?.closest<HTMLElement>(EDITOR_PARAGRAPH_SELECTOR);
  if (
    paragraph === null
    || paragraph === undefined
    || paragraph.dataset.surface !== surface
    || paragraph.dataset.editorParagraph === undefined
  ) return null;
  const fragments = [...paragraph.querySelectorAll<HTMLElement>(
    '[data-text-fragment="true"]',
  )];
  if (fragments.length === 0) {
    return paragraph.dataset.paragraphKind === SECTION_TITLE_KIND
      ? { paragraph_id: paragraph.dataset.editorParagraph, offset: 0, offset_encoding: "utf16" }
      : null;
  }
  if (document.createRange === undefined) return null;
  let best: { score: number; offset: number } | null = null;
  const candidates = fragments
    .map((fragment) => ({
      fragment,
      score: fragmentGeometryScore(fragment, x, y),
    }))
    .sort((left, right) => left.score - right.score)
    .slice(0, 8);
  for (const { fragment } of candidates) {
    const start = Number(fragment.dataset.utf16Start ?? fragment.dataset.start);
    const node = fragment.firstChild;
    if (!Number.isInteger(start) || start < 0 || node?.nodeType !== 3) continue;
    const text = node.textContent ?? "";
    if (text.length === 0) continue;
    const range = document.createRange();
    for (const offset of boundedGeometryOffsets(text)) {
      range.setStart(node, offset);
      range.collapse(true);
      const rect = range.getBoundingClientRect();
      if (
        rect.bottom > rect.top
        && Number.isFinite(rect.left + rect.right + rect.top + rect.bottom)
      ) {
        const horizontal = Math.max(rect.left - x, 0, x - rect.right);
        const vertical = Math.max(rect.top - y, 0, y - rect.bottom);
        const score = horizontal * horizontal + vertical * vertical;
        if (best === null || score < best.score) {
          best = { score, offset: start + offset };
        }
      }
    }
  }
  return best === null
    ? null
    : {
        paragraph_id: paragraph.dataset.editorParagraph,
        offset: best.offset,
      offset_encoding: "utf16",
    };
}

function fragmentGeometryScore(fragment: HTMLElement, x: number, y: number): number {
  const rects = typeof fragment.getClientRects === FUNCTION_TYPE
    ? Array.from(fragment.getClientRects())
    : typeof fragment.getBoundingClientRect === FUNCTION_TYPE
      ? [fragment.getBoundingClientRect()]
      : [];
  if (rects.length === 0) return Number.POSITIVE_INFINITY;
  return Math.min(...rects.map((rect) => geometryScore(rect, x, y)));
}

function geometryScore(rect: DOMRect | DOMRectReadOnly, x: number, y: number): number {
  const horizontal = Math.max(rect.left - x, 0, x - rect.right);
  const vertical = Math.max(rect.top - y, 0, y - rect.bottom);
  return horizontal * horizontal + vertical * vertical;
}

function boundedGeometryOffsets(text: string): number[] {
  const offsets = [0];
  for (let offset = 0; offset < text.length;) {
    const codePoint = text.codePointAt(offset);
    if (codePoint === undefined) break;
    offset += codePoint > 0xffff ? 2 : 1;
    offsets.push(offset);
  }
  if (offsets.length <= 25) return offsets;
  const bounded = new Set<number>([offsets[0]!, offsets[offsets.length - 1]!]);
  const samples = 23;
  for (let index = 1; index <= samples; index += 1) {
    const position = Math.round((index * (offsets.length - 1)) / (samples + 1));
    bounded.add(offsets[position]!);
  }
  return [...bounded].sort((left, right) => left - right);
}

function degradedOperationLabel(operation: "delete" | "move" | "insert" | null): string {
  return operation === "delete"
      ? "删除所选内容时"
      : operation === "move"
      ? "移动高亮内容时"
      : "从原稿加入时";
}

function isUncertainDraftDropFailure(error: unknown): boolean {
  // Structured HTTP errors are explicit server decisions. A rejected fetch
  // (typically TypeError) has no durable outcome and must fail closed.
  return !(
    typeof error === "object"
    && error !== null
    && "code" in error
    && typeof (error as { code?: unknown }).code === "string"
  );
}

export function visibleBylineForSurface(
  surface: DraftEditorSurface,
  person: {
    name: string | null;
    role: string | null;
    local_speaker_id: string | null;
  },
): string | null {
  if (surface === DRAFT_SURFACE) return null;
  const identity = [person.name, person.role].filter(
    (value): value is string => typeof value === "string" && value.length > 0,
  );
  return identity.join(" · ") || person.local_speaker_id || "未标注人物";
}

export function narrationStatusLabel(
  status: DraftEditorParagraph["narration_status"],
): string {
  return status === "recorded" ? "已绑定录音" : "待录音";
}

export function appendDraftTextRuns(
  target: HTMLElement,
  text: string,
  runs: readonly TextRun[],
  createCaret: () => HTMLElement,
  createElement: (tagName: "span") => HTMLElement = (tagName) => document.createElement(tagName),
): void {
  for (const run of runs) {
    if (run.caretBefore) target.append(createCaret());
    if (run.text.length === 0) continue;
    const fragment = createElement("span");
    fragment.className = fragmentClasses(run);
    fragment.dataset.textFragment = "true";
    fragment.dataset.start = String(run.start);
    fragment.dataset.end = String(run.end);
    fragment.dataset.utf16Start = String(codePointToUtf16(text, run.start));
    fragment.textContent = run.text;
    target.append(fragment);
  }
}

function fragmentClasses(run: TextRun): string {
  return [
    "draft-text-fragment",
    run.selected ? "is-selected" : "",
    run.searchMatch ? "is-search-match" : "",
    run.currentMatch ? "is-current-search" : "",
    run.correspondence ? "is-correspondence" : "",
    run.playback ? "is-playing" : "",
  ].filter(Boolean).join(" ");
}

function paragraphSignature(paragraph: DraftEditorParagraph): string {
  return JSON.stringify({
    kind: paragraph.kind,
    person: paragraph.person,
    text: paragraph.text,
    section_title: paragraph.section_title,
    narration_status: paragraph.narration_status,
    source_runs: paragraph.source_runs.map((run) => ({
      source_id: run.source_id,
      paragraph_id: run.paragraph_id,
      start_ticks: run.start_ticks,
      end_ticks: run.end_ticks,
      start_offset: run.start_offset,
      end_offset: run.end_offset,
      source_start_offset: run.source_start_offset,
      source_end_offset: run.source_end_offset,
      text: run.text,
    })),
  });
}

function findPanel(surface: DraftEditorSurface): HTMLElement {
  const panel = document.querySelector<HTMLElement>(
    `[data-draft-find-panel="${surface}"]`,
  );
  if (panel === null) throw new Error(`missing find panel ${surface}`);
  return panel;
}

function formatDuration(ticks: number): string {
  const seconds = Math.max(0, Math.round(ticks / TICKS_PER_SECOND));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes > 0 ? `${minutes} 分 ${remainder} 秒` : `${remainder} 秒`;
}

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (found === null) throw new Error(`missing #${id}`);
  return found as T;
}
