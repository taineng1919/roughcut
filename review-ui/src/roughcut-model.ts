import type { Clip, EditOperation, TranscriptSegment } from "./types";

const TICKS_PER_SECOND = 120_000;
const TICKS_PER_MILLISECOND = 120;

export interface RoughcutChangeSummary {
  deletedCount: number;
  restoredCount: number;
  orderChanged: boolean;
  trimmedClipIds: string[];
  clipCount: number;
  totalDurationTicks: number;
}

export interface ManuscriptRun {
  speakerKey: string;
  speakerLabel: string;
  clips: Array<{
    clip_id: string;
    source_id: string;
    display_text: string;
  }>;
}

export function parseReviewTime(value: string): number {
  const normalized = value.trim();
  if (normalized === "") throw new Error("请输入片段时间");
  const timecode = /^(\d+):([0-5]\d)(?:\.(\d{1,3}))?$/.exec(normalized);
  if (timecode !== null) {
    const minutes = Number(timecode[1]);
    const seconds = Number(timecode[2]);
    const milliseconds = fractionMilliseconds(timecode[3]);
    return safeTicks((minutes * 60 + seconds) * 1000 + milliseconds);
  }
  const seconds = /^(\d+)(?:\.(\d{1,3}))?$/.exec(normalized);
  if (seconds === null) {
    throw new Error("时间请使用秒数或“分:秒”格式");
  }
  const milliseconds = Number(seconds[1]) * 1000 + fractionMilliseconds(seconds[2]);
  return safeTicks(milliseconds);
}

export function formatReviewTime(ticks: number): string {
  if (!Number.isSafeInteger(ticks) || ticks < 0) throw new Error("片段时间无效");
  const milliseconds = Math.floor(ticks / TICKS_PER_MILLISECOND);
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.floor((milliseconds % 60_000) / 1000);
  const millis = milliseconds % 1000;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

export function trimOperationFromDisplay(
  clip: Clip,
  sourceIn: string,
  sourceOut: string,
): EditOperation {
  const sourceInTicks = parseReviewTime(sourceIn);
  const sourceOutTicks = parseReviewTime(sourceOut);
  if (sourceOutTicks <= sourceInTicks) {
    throw new Error("片段结束必须晚于片段开始");
  }
  return {
    type: "trim",
    clip_id: clip.clip_id,
    source_in_ticks: sourceInTicks,
    source_out_ticks: sourceOutTicks,
  };
}

export function validateReviewTrim(
  operation: EditOperation,
  clip: Clip,
  segment: TranscriptSegment | undefined,
  clips: readonly Clip[],
): void {
  if (operation.type !== "trim") throw new Error("当前操作不是片段边界调整");
  if (segment === undefined) throw new Error("无法读取当前片段的原话边界");
  if (
    operation.source_in_ticks < segment.start_ticks
    || operation.source_out_ticks > segment.end_ticks
  ) {
    throw new Error("调整不能超出这段原话的可用范围");
  }
  if (operation.source_out_ticks <= operation.source_in_ticks) {
    throw new Error("片段结束必须晚于片段开始");
  }
  for (const other of clips) {
    if (
      other.clip_id === clip.clip_id
      || other.source_id !== clip.source_id
      || other.transcript_version_id !== clip.transcript_version_id
      || other.segment_id !== clip.segment_id
    ) continue;
    const wasOverlapping = rangesOverlap(
      clip.source_in_ticks,
      clip.source_out_ticks,
      other.source_in_ticks,
      other.source_out_ticks,
    );
    const becomesOverlapping = rangesOverlap(
      operation.source_in_ticks,
      operation.source_out_ticks,
      other.source_in_ticks,
      other.source_out_ticks,
    );
    if (!wasOverlapping && becomesOverlapping) {
      throw new Error("调整后会与相邻片段重叠");
    }
  }
}

export function changeSummary(
  baseline: readonly Clip[],
  current: readonly Clip[],
): RoughcutChangeSummary {
  const baselineById = new Map(baseline.map((clip) => [clip.clip_id, clip]));
  const currentById = new Map(current.map((clip) => [clip.clip_id, clip]));
  const baselineShared = baseline
    .filter((clip) => currentById.has(clip.clip_id))
    .map((clip) => clip.clip_id);
  const currentShared = current
    .filter((clip) => baselineById.has(clip.clip_id))
    .map((clip) => clip.clip_id);
  return {
    deletedCount: baseline.filter((clip) => !currentById.has(clip.clip_id)).length,
    restoredCount: current.filter((clip) => !baselineById.has(clip.clip_id)).length,
    orderChanged: JSON.stringify(baselineShared) !== JSON.stringify(currentShared),
    trimmedClipIds: current
      .filter((clip) => {
        const original = baselineById.get(clip.clip_id);
        return original !== undefined
          && (
            original.source_in_ticks !== clip.source_in_ticks
            || original.source_out_ticks !== clip.source_out_ticks
          );
      })
      .map((clip) => clip.clip_id),
    clipCount: current.length,
    totalDurationTicks: current.reduce(
      (total, clip) => total + clip.source_out_ticks - clip.source_in_ticks,
      0,
    ),
  };
}

export function manuscriptRuns(
  clips: readonly Clip[],
  transcript: readonly TranscriptSegment[],
): ManuscriptRun[] {
  const segments = new Map(
    transcript.map((segment) => [
      `${segment.source_id}:${segment.transcript_version_id}:${segment.segment_id}`,
      segment,
    ]),
  );
  const runs: ManuscriptRun[] = [];
  for (const clip of clips) {
    const segment = segments.get(
      `${clip.source_id}:${clip.transcript_version_id}:${clip.segment_id}`,
    );
    const speakerKey = speakerIdentity(segment, clip);
    const speakerLabel = segment?.person_name
      ?? segment?.local_speaker_id
      ?? "人物未对应";
    let run = runs.at(-1);
    if (run === undefined || run.speakerKey !== speakerKey) {
      run = { speakerKey, speakerLabel, clips: [] };
      runs.push(run);
    }
    run.clips.push({
      clip_id: clip.clip_id,
      source_id: clip.source_id,
      display_text: clip.display_text,
    });
  }
  return runs;
}

function speakerIdentity(segment: TranscriptSegment | undefined, clip: Clip): string {
  if (segment?.person_id !== null && segment?.person_id !== undefined) {
    return `person:${segment.person_id}`;
  }
  if (segment?.local_speaker_id !== null && segment?.local_speaker_id !== undefined) {
    return `local:${clip.source_id}:${clip.transcript_version_id}:${segment.local_speaker_id}`;
  }
  return `unknown:${clip.source_id}:${clip.transcript_version_id}:${clip.segment_id}`;
}

function fractionMilliseconds(value: string | undefined): number {
  return Number((value ?? "").padEnd(3, "0") || "0");
}

function safeTicks(milliseconds: number): number {
  const ticks = milliseconds * TICKS_PER_MILLISECOND;
  if (!Number.isSafeInteger(ticks) || ticks < 0) throw new Error("片段时间超出范围");
  return ticks;
}

function rangesOverlap(
  leftIn: number,
  leftOut: number,
  rightIn: number,
  rightOut: number,
): boolean {
  return leftIn < rightOut && rightIn < leftOut;
}

export { TICKS_PER_SECOND };
