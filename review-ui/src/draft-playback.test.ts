import { describe, expect, it } from "vitest";

import {
  DraftPlaybackQueue,
  buildPlaybackSegments,
} from "./draft-playback";
import type { ResolvedSelectionRef } from "./workflow-types";

function ref(
  sourceId: string,
  segmentId: string,
  startTicks: number,
  endTicks: number,
): ResolvedSelectionRef {
  return {
    source_id: sourceId,
    transcript_version_id: `tr_${sourceId}`,
    segment_id: segmentId,
    start_ticks: startTicks,
    end_ticks: endTicks,
    canonical_text: segmentId,
  };
}

describe("ordered draft playback", () => {
  it("groups only touching refs and preserves cross-source manuscript order", () => {
    const segments = buildPlaybackSegments([
      ref("src_a", "a1", 0, 10),
      ref("src_a", "a2", 10, 20),
      ref("src_b", "b1", 100, 120),
      ref("src_a", "a3", 40, 50),
    ]);

    expect(segments).toEqual([
      {
        sourceId: "src_a",
        startTicks: 0,
        endTicks: 20,
        refCount: 2,
      },
      {
        sourceId: "src_b",
        startTicks: 100,
        endTicks: 120,
        refCount: 1,
      },
      {
        sourceId: "src_a",
        startTicks: 40,
        endTicks: 50,
        refCount: 1,
      },
    ]);
  });

  it("advances deterministically without treating compound refs as one media range", () => {
    const queue = new DraftPlaybackQueue();
    queue.replace([
      ref("src_a", "a1", 0, 10),
      ref("src_b", "b1", 20, 30),
    ]);

    expect(queue.current?.sourceId).toBe("src_a");
    expect(queue.advance()?.sourceId).toBe("src_b");
    expect(queue.advance()).toBeNull();
    expect(queue.finished).toBe(true);
  });
});
