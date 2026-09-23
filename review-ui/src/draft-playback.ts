import type { ResolvedSelectionRef } from "./workflow-types";

export interface DraftPlaybackSegment {
  sourceId: string;
  startTicks: number;
  endTicks: number;
  refCount: number;
}

export function buildPlaybackSegments(
  refs: readonly ResolvedSelectionRef[],
): DraftPlaybackSegment[] {
  const segments: DraftPlaybackSegment[] = [];
  for (const ref of refs) {
    const previous = segments.at(-1);
    if (
      previous !== undefined
      && previous.sourceId === ref.source_id
      && ref.start_ticks <= previous.endTicks
    ) {
      previous.endTicks = Math.max(previous.endTicks, ref.end_ticks);
      previous.refCount += 1;
    } else {
      segments.push({
        sourceId: ref.source_id,
        startTicks: ref.start_ticks,
        endTicks: ref.end_ticks,
        refCount: 1,
      });
    }
  }
  return segments;
}

export class DraftPlaybackQueue {
  private segments: DraftPlaybackSegment[] = [];
  private index = 0;

  get current(): DraftPlaybackSegment | null {
    return this.segments[this.index] ?? null;
  }

  get all(): readonly DraftPlaybackSegment[] {
    return this.segments;
  }

  get currentIndex(): number {
    return this.index;
  }

  get finished(): boolean {
    return this.segments.length > 0 && this.index >= this.segments.length;
  }

  replace(refs: readonly ResolvedSelectionRef[]): void {
    this.segments = buildPlaybackSegments(refs);
    this.index = 0;
  }

  advance(): DraftPlaybackSegment | null {
    this.index += 1;
    return this.current;
  }

  reset(): void {
    this.segments = [];
    this.index = 0;
  }
}
