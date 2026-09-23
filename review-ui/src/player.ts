import type { ReviewSource, TimelineSpan, TranscriptSegment } from "./types";

export const TICKS_PER_SECOND = 120_000;

export function playbackLabel(source: ReviewSource): string {
  if (source.playback_kind === "original") return "原素材";
  const profile = source.proxy_profile;
  if (profile === null) return "代理";
  const rate = profile.frame_rate.denominator === 1
    ? String(profile.frame_rate.numerator)
    : `${profile.frame_rate.numerator}/${profile.frame_rate.denominator}`;
  return `代理 ${profile.canvas.width}×${profile.canvas.height} / ${rate}fps`;
}

export function mediaFailureMessage(source: ReviewSource): string {
  if (source.playback_kind === "proxy") {
    return `素材 ${source.display_name} 的已选代理已失效。请返回 Agent 重新执行 proxy_create，随后重新启动 Review。`;
  }
  return `浏览器无法播放原素材 ${source.display_name}。请返回 Agent 执行 proxy_create，随后重新启动 Review；页面不会自动转码或切换。`;
}

export interface PlaybackPosition {
  clipIndex: number;
  clipId: string;
  sourceId: string;
  sourceTicks: number;
  outputTicks: number;
}

export function positionForOutput(spans: readonly TimelineSpan[], outputTicks: number): PlaybackPosition {
  const final = spans.at(-1);
  if (final !== undefined && outputTicks === final.output_out_ticks) {
    return {
      clipIndex: spans.length - 1,
      clipId: final.clip_id,
      sourceId: final.source_id,
      sourceTicks: final.source_out_ticks,
      outputTicks,
    };
  }
  const clipIndex = spans.findIndex(
    (span) => span.output_in_ticks <= outputTicks && outputTicks < span.output_out_ticks,
  );
  if (clipIndex < 0) {
    throw new Error("output time is outside the server timeline");
  }
  const span = spans[clipIndex];
  if (span === undefined) {
    throw new Error("timeline span is missing");
  }
  return {
    clipIndex,
    clipId: span.clip_id,
    sourceId: span.source_id,
    sourceTicks: span.source_in_ticks + outputTicks - span.output_in_ticks,
    outputTicks,
  };
}

export function positionForClip(spans: readonly TimelineSpan[], clipId: string): PlaybackPosition {
  const clipIndex = spans.findIndex((span) => span.clip_id === clipId);
  const span = spans[clipIndex];
  if (clipIndex < 0 || span === undefined) {
    throw new Error("clip is absent from the server timeline");
  }
  return {
    clipIndex,
    clipId,
    sourceId: span.source_id,
    sourceTicks: span.source_in_ticks,
    outputTicks: span.output_in_ticks,
  };
}

export function positionForTranscript(
  spans: readonly TimelineSpan[],
  segments: readonly TranscriptSegment[],
  sourceId: string,
  transcriptVersionId: string,
  segmentId: string,
): PlaybackPosition {
  const segment = segments.find(
    (item) => item.source_id === sourceId
      && item.transcript_version_id === transcriptVersionId
      && item.segment_id === segmentId,
  );
  if (segment === undefined) {
    throw new Error("unknown transcript segment");
  }
  const clipIndex = spans.findIndex(
    (span) => span.source_id === sourceId
      && span.source_in_ticks < segment.end_ticks
      && segment.start_ticks < span.source_out_ticks,
  );
  const span = spans[clipIndex];
  if (clipIndex >= 0 && span !== undefined) {
    const sourceTicks = Math.max(segment.start_ticks, span.source_in_ticks);
    return {
      clipIndex,
      clipId: span.clip_id,
      sourceId,
      sourceTicks,
      outputTicks: span.output_in_ticks + sourceTicks - span.source_in_ticks,
    };
  }
  return {
    clipIndex: -1,
    clipId: "",
    sourceId,
    sourceTicks: segment.start_ticks,
    outputTicks: 0,
  };
}

export function transitionAtBoundary(
  spans: readonly TimelineSpan[],
  clipIndex: number,
  sourceTicks: number,
): { action: "stay" | "advance" | "ended"; position: PlaybackPosition | null } {
  const span = spans[clipIndex];
  if (span === undefined) {
    throw new Error("active clip is outside the timeline");
  }
  if (sourceTicks < span.source_out_ticks) {
    return {
      action: "stay",
      position: {
        clipIndex,
        clipId: span.clip_id,
        sourceId: span.source_id,
        sourceTicks,
        outputTicks: span.output_in_ticks + sourceTicks - span.source_in_ticks,
      },
    };
  }
  const next = spans[clipIndex + 1];
  if (next === undefined) {
    return { action: "ended", position: null };
  }
  return {
    action: "advance",
    position: {
      clipIndex: clipIndex + 1,
      clipId: next.clip_id,
      sourceId: next.source_id,
      sourceTicks: next.source_in_ticks,
      outputTicks: next.output_in_ticks,
    },
  };
}

export function ticksToSeconds(ticks: number): number {
  return ticks / TICKS_PER_SECOND;
}
