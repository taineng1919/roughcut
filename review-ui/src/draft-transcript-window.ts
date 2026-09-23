import type { DraftTranscriptWindow, ReadableParagraph } from "./workflow-types";

export const TRANSCRIPT_WINDOW_LIMIT = 48;
export const TRANSCRIPT_ROW_ESTIMATE = 94;

export interface TranscriptWindowRequest {
  sourceId: string;
  offset: number;
  paragraphId?: string;
}

export interface AdjacentTranscriptWindowRequest {
  direction: "prepend" | "append";
  request: TranscriptWindowRequest;
}

export class DraftTranscriptWindowState {
  sourceId: string;
  offset = 0;
  total = 0;
  paragraphs: ReadableParagraph[] = [];
  locatedParagraphId: string | null = null;

  constructor(sourceId: string) {
    this.sourceId = sourceId;
  }

  apply(window: DraftTranscriptWindow): void {
    if (window.source_id !== this.sourceId) {
      throw new Error("原稿窗口素材与当前素材不一致");
    }
    this.offset = window.offset;
    this.total = window.total;
    this.paragraphs = window.paragraphs;
    this.locatedParagraphId = window.located_paragraph_id;
  }

  extend(
    window: DraftTranscriptWindow,
    direction: "prepend" | "append",
  ): void {
    if (window.source_id !== this.sourceId || window.total !== this.total) {
      throw new Error("相邻原稿窗口与当前素材不一致");
    }
    if (
      direction === "prepend"
      && window.offset + window.paragraphs.length !== this.offset
    ) {
      throw new Error("前一原稿窗口与当前窗口不连续");
    }
    if (
      direction === "append"
      && window.offset !== this.offset + this.paragraphs.length
    ) {
      throw new Error("后一原稿窗口与当前窗口不连续");
    }
    if (direction === "prepend") {
      this.offset = window.offset;
      this.paragraphs = [...window.paragraphs, ...this.paragraphs];
    } else {
      this.paragraphs = [...this.paragraphs, ...window.paragraphs];
    }
    this.locatedParagraphId = window.located_paragraph_id;
  }

  reset(sourceId: string): TranscriptWindowRequest {
    this.sourceId = sourceId;
    this.offset = 0;
    this.total = 0;
    this.paragraphs = [];
    this.locatedParagraphId = null;
    return { sourceId, offset: 0 };
  }

  locate(sourceId: string, paragraphId: string): TranscriptWindowRequest {
    if (sourceId !== this.sourceId) this.reset(sourceId);
    return { sourceId, offset: 0, paragraphId };
  }

  requestForScroll(scrollTop: number): TranscriptWindowRequest | null {
    if (this.total <= TRANSCRIPT_WINDOW_LIMIT) return null;
    const visibleIndex = Math.max(0, Math.floor(scrollTop / TRANSCRIPT_ROW_ESTIMATE));
    const buffer = Math.floor(TRANSCRIPT_WINDOW_LIMIT / 4);
    const windowEnd = this.offset + this.paragraphs.length;
    if (visibleIndex >= this.offset + buffer && visibleIndex < windowEnd - buffer) {
      return null;
    }
    const offset = Math.min(
      Math.max(visibleIndex - Math.floor(TRANSCRIPT_WINDOW_LIMIT / 2), 0),
      Math.max(this.total - TRANSCRIPT_WINDOW_LIMIT, 0),
    );
    return offset === this.offset ? null : { sourceId: this.sourceId, offset };
  }

  requestForSelectionScroll(
    scrollTop: number,
  ): AdjacentTranscriptWindowRequest | null {
    const visibleIndex = Math.max(0, Math.floor(scrollTop / TRANSCRIPT_ROW_ESTIMATE));
    const threshold = 4;
    const windowEnd = this.offset + this.paragraphs.length;
    if (visibleIndex >= windowEnd - threshold && windowEnd < this.total) {
      return {
        direction: "append",
        request: { sourceId: this.sourceId, offset: windowEnd },
      };
    }
    if (visibleIndex <= this.offset + threshold && this.offset > 0) {
      return {
        direction: "prepend",
        request: {
          sourceId: this.sourceId,
          offset: Math.max(0, this.offset - TRANSCRIPT_WINDOW_LIMIT),
        },
      };
    }
    return null;
  }

  topSpacerHeight(): number {
    return this.offset * TRANSCRIPT_ROW_ESTIMATE;
  }

  bottomSpacerHeight(): number {
    return Math.max(
      0,
      (this.total - this.offset - this.paragraphs.length) * TRANSCRIPT_ROW_ESTIMATE,
    );
  }
}
