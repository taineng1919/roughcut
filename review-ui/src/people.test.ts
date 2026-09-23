import { describe, expect, it } from "vitest";

import { speakerLabel } from "./people";
import type { TranscriptSegment } from "./types";

function segment(sourceId: string, personId: string, personName: string): TranscriptSegment {
  return {
    source_id: sourceId,
    transcript_version_id: `tr_${sourceId}`,
    segment_id: "seg_shared",
    text: sourceId,
    original_text: sourceId,
    corrected_text: null,
    start_ticks: 0,
    end_ticks: 120_000,
    speaker: personName,
    local_speaker_id: "spk_0",
    person_id: personId,
    person_name: personName,
  };
}

describe("source-scoped speaker display", () => {
  it("shows different confirmed people for equal local speaker IDs", () => {
    const sourceA = segment("src_a", "person_a", "人物 A");
    const sourceB = segment("src_b", "person_b", "人物 B");

    expect(sourceA.local_speaker_id).toBe(sourceB.local_speaker_id);
    expect(speakerLabel(sourceA)).toBe("人物 A");
    expect(speakerLabel(sourceB)).toBe("人物 B");
  });
});
