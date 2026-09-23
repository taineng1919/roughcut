import { describe, expect, it } from "vitest";

import {
  changeSummary,
  formatReviewTime,
  manuscriptRuns,
  parseReviewTime,
  trimOperationFromDisplay,
  validateReviewTrim,
} from "./roughcut-model";
import type { Clip, TranscriptSegment } from "./types";

const clips: Clip[] = [
  {
    clip_id: "clip_a_open",
    source_id: "src_a",
    transcript_version_id: "tr_a",
    segment_id: "seg_a_open",
    source_in_ticks: 0,
    source_out_ticks: 240_000,
    reason: "开场",
    display_text: "欢迎来到校园。",
  },
  {
    clip_id: "clip_b",
    source_id: "src_b",
    transcript_version_id: "tr_b",
    segment_id: "seg_b",
    source_in_ticks: 120_000,
    source_out_ticks: 360_000,
    reason: "课程",
    display_text: "这里有丰富的课程。",
  },
  {
    clip_id: "clip_a_return",
    source_id: "src_a",
    transcript_version_id: "tr_a",
    segment_id: "seg_a_return",
    source_in_ticks: 480_000,
    source_out_ticks: 720_000,
    reason: "收束",
    display_text: "期待再次见面。",
  },
];

const transcript: TranscriptSegment[] = [
  {
    source_id: "src_a",
    transcript_version_id: "tr_a",
    segment_id: "seg_a_open",
    text: "欢迎来到校园。",
    original_text: "欢迎来到校园。",
    corrected_text: null,
    start_ticks: 0,
    end_ticks: 240_000,
    speaker: "小严",
    local_speaker_id: "spk_0",
    person_id: "person_yan",
    person_name: "小严",
  },
  {
    source_id: "src_b",
    transcript_version_id: "tr_b",
    segment_id: "seg_b",
    text: "这里有丰富的课程。",
    original_text: "这里有丰富的课程。",
    corrected_text: null,
    start_ticks: 120_000,
    end_ticks: 360_000,
    speaker: "小赵",
    local_speaker_id: "spk_0",
    person_id: "person_zhao",
    person_name: "小赵",
  },
  {
    source_id: "src_a",
    transcript_version_id: "tr_a",
    segment_id: "seg_a_return",
    text: "期待再次见面。",
    original_text: "期待再次见面。",
    corrected_text: null,
    start_ticks: 480_000,
    end_ticks: 720_000,
    speaker: "小严",
    local_speaker_id: "spk_0",
    person_id: "person_yan",
    person_name: "小严",
  },
];

describe("roughcut review time entry", () => {
  it("accepts seconds and readable timecodes without floating-point drift", () => {
    expect(parseReviewTime("0")).toBe(0);
    expect(parseReviewTime("3.5")).toBe(420_000);
    expect(parseReviewTime("00:03.500")).toBe(420_000);
    expect(parseReviewTime("1:02.25")).toBe(7_470_000);
    expect(formatReviewTime(7_470_000)).toBe("01:02.250");
  });

  it("rejects negative, ambiguous, or over-precise values", () => {
    for (const value of ["", "-1", "1:2:3", "1:60", "1.2345", "abc"]) {
      expect(() => parseReviewTime(value)).toThrow();
    }
  });

  it("builds bounded trim operations from user-facing time values", () => {
    const operation = trimOperationFromDisplay(clips[0]!, "00:00.100", "00:01.900");
    expect(operation).toEqual({
      type: "trim",
      clip_id: "clip_a_open",
      source_in_ticks: 12_000,
      source_out_ticks: 228_000,
    });
    expect(() => validateReviewTrim(operation, clips[0]!, transcript[0], clips))
      .not.toThrow();
    expect(() => trimOperationFromDisplay(clips[0]!, "00:01.000", "00:01.000"))
      .toThrow(/片段结束必须晚于片段开始/);
    expect(() => validateReviewTrim(
      {
        type: "trim",
        clip_id: "clip_a_open",
        source_in_ticks: 12_000,
        source_out_ticks: 252_000,
      },
      clips[0]!,
      transcript[0],
      clips,
    )).toThrow(/原话/);
  });
});

describe("roughcut manuscript and change summary", () => {
  it("keeps A→B→A clip identity while presenting continuous speaker runs", () => {
    const runs = manuscriptRuns(clips, transcript);
    expect(runs.map((run) => run.speakerLabel)).toEqual(["小严", "小赵", "小严"]);
    expect(runs.flatMap((run) => run.clips.map((clip) => clip.clip_id))).toEqual([
      "clip_a_open",
      "clip_b",
      "clip_a_return",
    ]);
  });

  it("summarizes net changes relative to the page-entry snapshot", () => {
    const changed: Clip[] = [
      { ...clips[2]!, source_in_ticks: 492_000 },
      clips[0]!,
      {
        clip_id: "clip_restored",
        source_id: "src_b",
        transcript_version_id: "tr_b",
        segment_id: "seg_restored",
        source_in_ticks: 360_000,
        source_out_ticks: 480_000,
        reason: "恢复",
        display_text: "恢复内容。",
      },
    ];
    expect(changeSummary(clips, changed)).toEqual({
      deletedCount: 1,
      restoredCount: 1,
      orderChanged: true,
      trimmedClipIds: ["clip_a_return"],
      clipCount: 3,
      totalDurationTicks: 588_000,
    });
  });
});
