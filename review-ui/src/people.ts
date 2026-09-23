import type { TranscriptSegment } from "./types";

export function speakerLabel(segment: TranscriptSegment): string {
  return segment.person_name ?? segment.local_speaker_id ?? "未标注";
}
