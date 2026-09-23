import type {
  ActiveTranscriptSegment,
  EditReferenceStatus,
  ReviewSessionState,
} from "./types";

export function displayedText(segment: ActiveTranscriptSegment): {
  text: string;
  status: "使用原文" | "已校正";
} {
  return segment.corrected_text === null
    ? { text: segment.original_text, status: "使用原文" }
    : { text: segment.corrected_text, status: "已校正" };
}

export function normalizeCorrection(value: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) throw new Error("校正文不能为空");
  return normalized;
}

export function correctionPayload(
  segmentId: string,
  value: string | null,
): { segment_id: string; corrected_text: string | null } {
  return {
    segment_id: segmentId,
    corrected_text: value === null ? null : normalizeCorrection(value),
  };
}

export function editReferenceMessage(
  status: EditReferenceStatus,
  sourceNames: ReadonlyMap<string, string>,
): string {
  if (status.status === "none") return "当前没有已确认剪辑。";
  if (status.status === "current") return "已确认剪辑与当前转录稿一致。";
  const mismatches = status.mismatches.map((mismatch) => {
    const name = sourceNames.get(mismatch.source_id) ?? mismatch.source_id;
    return name;
  });
  return `以下素材的转录稿已经变化，需要重新确认剪辑：${mismatches.join("、")}`;
}

export function reviewSessionMessage(session: ReviewSessionState): string {
  if (!session.read_only) return "当前页面内容可正常使用。";
  return "项目内容已在别处更新，本页已转为只读，请重新打开当前步骤。";
}
