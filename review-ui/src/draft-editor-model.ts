import type {
  DraftEditorCaret,
  DraftEditorSourceRun,
  DraftEditorSelectionRequest,
  DraftEditorSelectionResponse,
} from "./workflow-types";

export type DraftEditorSurface = "draft" | "source";
export type DraftEditorPane = DraftEditorSurface | "player";

export interface EditorTextRange {
  paragraphId: string;
  start: number;
  end: number;
}

export function sourceRangeForRun(run: DraftEditorSourceRun): EditorTextRange {
  return {
    paragraphId: run.paragraph_id,
    start: run.source_start_offset,
    end: run.source_end_offset,
  };
}

export function optionalDisplayTitle(value: string | null | undefined): string | null {
  const normalized = value?.trim() ?? "";
  return normalized.length > 0 ? normalized : null;
}

export interface ActiveSelection {
  surface: DraftEditorSurface;
  request: DraftEditorSelectionRequest;
  response: DraftEditorSelectionResponse;
  acceptedDegraded: boolean;
}

export interface SourceLocation {
  sourceId: string;
  paragraphId: string | null;
}

export interface TextDecoration {
  selected: EditorTextRange | null;
  matches: { start: number; end: number; current: boolean }[];
  correspondence: EditorTextRange | null;
  playback: EditorTextRange | null;
  caret: number | null;
  boundaries?: number[];
}

export interface TextRun {
  text: string;
  start: number;
  end: number;
  selected: boolean;
  searchMatch: boolean;
  currentMatch: boolean;
  correspondence: boolean;
  playback: boolean;
  caretBefore: boolean;
}

export class DraftEditorViewState {
  currentSourceId: string;
  mobilePane: DraftEditorPane = "draft";
  focusedSurface: DraftEditorSurface = "draft";
  draftMode: "body" | "sections" = "body";
  caret: DraftEditorCaret | null = null;
  draftSelection: ActiveSelection | null = null;
  sourceSelection: ActiveSelection | null = null;
  sourceLocation: SourceLocation;
  lastSelectionSurface: DraftEditorSurface = "draft";
  locked = false;
  search: Record<DraftEditorSurface, { query: string; current: number }> = {
    draft: { query: "", current: 0 },
    source: { query: "", current: 0 },
  };

  constructor(sourceId: string) {
    this.currentSourceId = sourceId;
    this.sourceLocation = { sourceId, paragraphId: null };
  }

  selectSource(sourceId: string): void {
    this.currentSourceId = sourceId;
    this.sourceLocation = { sourceId, paragraphId: null };
    this.focusedSurface = "source";
  }

  selectPane(pane: DraftEditorPane): void {
    this.mobilePane = pane;
    if (pane !== "player") this.focusedSurface = pane;
  }

  setCaret(caret: DraftEditorCaret | null): void {
    this.caret = caret === null ? null : copied(caret);
  }

  selectionFor(surface: DraftEditorSurface): ActiveSelection | null {
    return surface === "draft" ? this.draftSelection : this.sourceSelection;
  }

  setSelection(
    surface: DraftEditorSurface,
    selection: ActiveSelection | null,
  ): void {
    const copiedSelection = selection === null ? null : copied(selection);
    if (surface === "draft") {
      this.draftSelection = copiedSelection;
      if (copiedSelection !== null) this.sourceSelection = null;
    } else {
      this.sourceSelection = copiedSelection;
      if (copiedSelection !== null) this.draftSelection = null;
    }
    if (selection !== null) this.lastSelectionSurface = surface;
  }

  clearSelections(): void {
    this.draftSelection = null;
    this.sourceSelection = null;
  }

  setSourceLocation(location: SourceLocation): void {
    this.currentSourceId = location.sourceId;
    this.sourceLocation = copied(location);
  }

  setDraftMode(mode: "body" | "sections"): void {
    this.draftMode = mode;
  }

  setSearch(surface: DraftEditorSurface, query: string, current = 0): void {
    this.search[surface] = { query, current };
  }
}

export function codePointToUtf16(text: string, offset: number): number {
  if (!Number.isInteger(offset) || offset < 0 || offset > Array.from(text).length) {
    throw new Error("文字位置超出段落范围");
  }
  return Array.from(text).slice(0, offset).join("").length;
}

export function utf16ToCodePoint(text: string, offset: number): number {
  if (!Number.isInteger(offset) || offset < 0 || offset > text.length) {
    throw new Error("文字位置超出段落范围");
  }
  if (
    offset > 0
    && offset < text.length
    && isHighSurrogate(text.charCodeAt(offset - 1))
    && isLowSurrogate(text.charCodeAt(offset))
  ) {
    throw new Error("文字位置不能落在 Unicode 字符中间");
  }
  return Array.from(text.slice(0, offset)).length;
}

export function orderedRange(
  range: DraftEditorSelectionResponse["display_range"],
): { start: DraftEditorDisplayEndpoint; end: DraftEditorDisplayEndpoint } {
  const anchor = endpoint(range.anchor);
  const focus = endpoint(range.focus);
  return compareEndpoint(anchor, focus) <= 0
    ? { start: anchor, end: focus }
    : { start: focus, end: anchor };
}

export function caretOffsetForParagraph(
  caret: DraftEditorCaret | null,
  paragraphId: string,
): number | null {
  return caret?.paragraph_id === paragraphId ? caret.character_offset : null;
}

export interface ContiguousPatch {
  prefixCount: number;
  suffixCount: number;
}

export function contiguousPatch<T>(
  before: readonly T[],
  after: readonly T[],
  signature: (value: T) => string,
): ContiguousPatch {
  let prefixCount = 0;
  while (
    prefixCount < before.length
    && prefixCount < after.length
    && signature(before[prefixCount]!) === signature(after[prefixCount]!)
  ) {
    prefixCount += 1;
  }
  let suffixCount = 0;
  while (
    suffixCount < before.length - prefixCount
    && suffixCount < after.length - prefixCount
    && signature(before[before.length - 1 - suffixCount]!)
      === signature(after[after.length - 1 - suffixCount]!)
  ) {
    suffixCount += 1;
  }
  return { prefixCount, suffixCount };
}

export interface DraftEditorDisplayEndpoint {
  paragraphId: string;
  characterOffset: number;
  utf16Offset: number;
}

export type DraftEditorDisplayRange = DraftEditorSelectionResponse["display_range"];

export function displayRangeForParagraph(
  range: DraftEditorDisplayRange,
  paragraphId: string,
  text: string,
  order: string[],
): EditorTextRange | null {
  const anchorIndex = order.indexOf(range.anchor.paragraph_id);
  const focusIndex = order.indexOf(range.focus.paragraph_id);
  const paragraphIndex = order.indexOf(paragraphId);
  if (anchorIndex < 0 || focusIndex < 0) return null;
  const forward = anchorIndex < focusIndex
    || anchorIndex === focusIndex
      && range.anchor.character_offset <= range.focus.character_offset;
  const start = forward ? range.anchor : range.focus;
  const end = forward ? range.focus : range.anchor;
  const startIndex = forward ? anchorIndex : focusIndex;
  const endIndex = forward ? focusIndex : anchorIndex;
  if (paragraphIndex < startIndex || paragraphIndex > endIndex) return null;
  const length = Array.from(text).length;
  return {
    paragraphId,
    start: paragraphIndex === startIndex
      ? start.character_offset
      : 0,
    end: paragraphIndex === endIndex
      ? end.character_offset
      : length,
  };
}

export function paragraphIdsForDisplayRange(
  range: DraftEditorDisplayRange,
  order: string[],
): string[] {
  const anchor = order.indexOf(range.anchor.paragraph_id);
  const focus = order.indexOf(range.focus.paragraph_id);
  if (anchor < 0 || focus < 0) return [];
  return order.slice(Math.min(anchor, focus), Math.max(anchor, focus) + 1);
}

export function partitionText(
  text: string,
  paragraphId: string,
  decoration: TextDecoration,
): TextRun[] {
  const length = Array.from(text).length;
  const boundaries = new Set<number>([0, length]);
  const ranges = [
    ...(decoration.selected === null ? [] : [decoration.selected]),
    ...decoration.matches.map((match) => ({ paragraphId, ...match })),
    ...(decoration.correspondence === null ? [] : [decoration.correspondence]),
    ...(decoration.playback === null ? [] : [decoration.playback]),
  ].filter((range) => range.paragraphId === paragraphId);
  for (const range of ranges) {
    boundaries.add(clamp(range.start, length));
    boundaries.add(clamp(range.end, length));
  }
  if (decoration.caret !== null) boundaries.add(clamp(decoration.caret, length));
  for (const boundary of decoration.boundaries ?? []) {
    boundaries.add(clamp(boundary, length));
  }
  const points = [...boundaries].sort((left, right) => left - right);
  const characters = Array.from(text);
  const runs = points.slice(0, -1).map((start, index) => {
    const end = points[index + 1]!;
    const matches = decoration.matches.filter(
      (match) => start < match.end && match.start < end,
    );
    const selected = overlaps(decoration.selected, paragraphId, start, end);
    return {
      text: characters.slice(start, end).join(""),
      start,
      end,
      selected,
      searchMatch: matches.length > 0,
      currentMatch: matches.some((match) => match.current),
      correspondence: overlaps(decoration.correspondence, paragraphId, start, end),
      playback: overlaps(decoration.playback, paragraphId, start, end),
      caretBefore: decoration.caret === start,
    };
  });
  if (decoration.caret === length) {
    runs.push({
      text: "",
      start: length,
      end: length,
      selected: false,
      searchMatch: false,
      currentMatch: false,
      correspondence: false,
      playback: false,
      caretBefore: true,
    });
  }
  return runs;
}

function endpoint(value: DraftEditorSelectionResponse["display_range"]["anchor"]): DraftEditorDisplayEndpoint {
  return {
    paragraphId: value.paragraph_id,
    characterOffset: value.character_offset,
    utf16Offset: value.utf16_offset,
  };
}

function compareEndpoint(left: DraftEditorDisplayEndpoint, right: DraftEditorDisplayEndpoint): number {
  return left.paragraphId === right.paragraphId
    ? left.characterOffset - right.characterOffset
    : left.paragraphId.localeCompare(right.paragraphId);
}

function overlaps(
  range: EditorTextRange | null,
  paragraphId: string,
  start: number,
  end: number,
): boolean {
  return range !== null
    && range.paragraphId === paragraphId
    && start < range.end
    && range.start < end;
}

function clamp(value: number, length: number): number {
  return Math.max(0, Math.min(value, length));
}

function isHighSurrogate(value: number): boolean {
  return 0xD800 <= value && value <= 0xDBFF;
}

function isLowSurrogate(value: number): boolean {
  return 0xDC00 <= value && value <= 0xDFFF;
}

function copied<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}
