import { describe, expect, it } from "vitest";

import {
  mediaFailureMessage,
  playbackLabel,
  positionForClip,
  positionForOutput,
  positionForTranscript,
  transitionAtBoundary,
} from "./player";
import type { ReviewSource, TimelineSpan, TranscriptSegment } from "./types";

const originalSource: ReviewSource = {
  source_id: "src_a",
  transcript_version_id: "tr_a",
  display_name: "素材 A.mp4",
  kind: "video",
  duration_ticks: 1_200_000,
  tags: [],
  note: "",
  media_url: "/media/src_a",
  playback_kind: "original",
  proxy_profile: null,
};

const proxySource: ReviewSource = {
  ...originalSource,
  source_id: "src_b",
  display_name: "素材 B.mov",
  media_url: "/media/src_b",
  playback_kind: "proxy",
  proxy_profile: {
    canvas: { width: 1280, height: 720 },
    frame_rate: { numerator: 25, denominator: 1 },
    has_audio: true,
  },
};

const spans: TimelineSpan[] = Array.from({ length: 10 }, (_, index) => ({
  clip_id: `clip_${index + 1}`,
  source_id: "src_a",
  source_in_ticks: index * 240_000 + 60_000,
  source_out_ticks: index * 240_000 + 180_000,
  output_in_ticks: index * 120_000,
  output_out_ticks: (index + 1) * 120_000,
}));
const transcript: TranscriptSegment[] = spans.map((span, index) => ({
  source_id: "src_a",
  transcript_version_id: "tr_a",
  segment_id: `seg_${index + 1}`,
  text: `第${index + 1}句`,
  original_text: `第${index + 1}句`,
  corrected_text: null,
  start_ticks: span.source_in_ticks,
  end_ticks: span.source_out_ticks,
  speaker: null,
  local_speaker_id: null,
  person_id: null,
  person_name: null,
}));

const multiSpans: TimelineSpan[] = [
  { clip_id: "clip_a_open", source_id: "src_a", source_in_ticks: 0, source_out_ticks: 120_000, output_in_ticks: 0, output_out_ticks: 120_000 },
  { clip_id: "clip_b", source_id: "src_b", source_in_ticks: 0, source_out_ticks: 120_000, output_in_ticks: 120_000, output_out_ticks: 240_000 },
  { clip_id: "clip_a_return", source_id: "src_a", source_in_ticks: 240_000, source_out_ticks: 360_000, output_in_ticks: 240_000, output_out_ticks: 360_000 },
];

const multiTranscript: TranscriptSegment[] = [
  { source_id: "src_a", transcript_version_id: "tr_a", segment_id: "seg_shared", text: "A", original_text: "A", corrected_text: null, start_ticks: 0, end_ticks: 120_000, speaker: "人物 A", local_speaker_id: "spk_0", person_id: "person_a", person_name: "人物 A" },
  { source_id: "src_b", transcript_version_id: "tr_b", segment_id: "seg_shared", text: "B", original_text: "B", corrected_text: null, start_ticks: 0, end_ticks: 120_000, speaker: "人物 B", local_speaker_id: "spk_0", person_id: "person_b", person_name: "人物 B" },
  { source_id: "src_a", transcript_version_id: "tr_a", segment_id: "seg_return", text: "A return", original_text: "A return", corrected_text: null, start_ticks: 240_000, end_ticks: 360_000, speaker: "人物 A", local_speaker_id: "spk_0", person_id: "person_a", person_name: "人物 A" },
];

describe("server-derived virtual playback", () => {
  it("labels frozen playback and gives distinct actionable failures", () => {
    expect(playbackLabel(originalSource)).toBe("原素材");
    expect(playbackLabel(proxySource)).toBe("代理 1280×720 / 25fps");
    expect(mediaFailureMessage(originalSource)).toContain("proxy_create");
    expect(mediaFailureMessage(originalSource)).toContain("重新启动 Review");
    expect(mediaFailureMessage(proxySource)).toContain("代理已失效");
    expect(mediaFailureMessage(proxySource)).not.toContain("自动");
  });

  it("seeks clips, transcript and exact output boundaries", () => {
    expect(positionForClip(spans, "clip_4")).toMatchObject({
      clipIndex: 3,
      sourceTicks: 780_000,
      outputTicks: 360_000,
    });
    expect(positionForTranscript(spans, transcript, "src_a", "tr_a", "seg_7")).toMatchObject({
      clipIndex: 6,
      clipId: "clip_7",
      sourceId: "src_a",
      sourceTicks: 1_500_000,
    });
    expect(positionForOutput(spans, 120_000)).toMatchObject({
      clipIndex: 1,
      clipId: "clip_2",
      sourceTicks: 300_000,
    });
    expect(positionForOutput(spans, 1_200_000)).toMatchObject({
      clipIndex: 9,
      clipId: "clip_10",
      sourceTicks: spans[9]!.source_out_ticks,
      outputTicks: 1_200_000,
    });
  });

  it("keeps equal segment IDs source-scoped and returns to the second A clip", () => {
    expect(originalSource.duration_ticks).toBeGreaterThan(
      multiSpans.at(-1)!.output_out_ticks,
    );
    expect(positionForOutput(multiSpans, 359_999)).toMatchObject({
      clipIndex: 2,
      clipId: "clip_a_return",
      sourceId: "src_a",
      outputTicks: 359_999,
    });
    expect(positionForTranscript(multiSpans, multiTranscript, "src_b", "tr_b", "seg_shared")).toMatchObject({
      clipIndex: 1,
      clipId: "clip_b",
      sourceId: "src_b",
    });
    expect(positionForClip(multiSpans, "clip_a_return")).toMatchObject({
      clipIndex: 2,
      sourceId: "src_a",
      sourceTicks: 240_000,
      outputTicks: 240_000,
    });
    expect(transitionAtBoundary(multiSpans, 0, 120_000).position).toMatchObject({
      clipId: "clip_b",
      sourceId: "src_b",
    });
    expect(transitionAtBoundary(multiSpans, 1, 120_000).position).toMatchObject({
      clipId: "clip_a_return",
      sourceId: "src_a",
      sourceTicks: 240_000,
    });
  });

  it("advances through all ten clips and ends at the final boundary", () => {
    for (let index = 0; index < 9; index += 1) {
      const result = transitionAtBoundary(spans, index, spans[index]!.source_out_ticks);
      expect(result.action).toBe("advance");
      expect(result.position?.clipIndex).toBe(index + 1);
      expect(result.position?.outputTicks).toBe((index + 1) * 120_000);
    }
    expect(transitionAtBoundary(spans, 9, spans[9]!.source_out_ticks)).toEqual({
      action: "ended",
      position: null,
    });
  });
});
