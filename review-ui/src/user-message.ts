const TECHNICAL_TERMS = [
  /Brief|Content Draft|Readable Transcript|Proposal|Decision|Render/i,
  /candidate|current|stale|workflow|overlay|session|schema|revision|hash|artifact/i,
  /\bEdit\b|\bclip\b|\bticks?\b|\bProject\b|\bTranscript\b/i,
  /source_id|transcript_version_id|segment_id|block_id|source binding/i,
  /paragraph(?:_id| identity)?|\bcursor\b/i,
  /checkpoint|workspace|generation|operation ID/i,
];

const GENERIC_OPERATION_ERROR =
  "操作未完成。项目内容或页面状态可能已经变化，请重新打开当前步骤后再试。";

export function userFacingErrorMessage(error: unknown): string {
  const detailMessage = typeof error === "object" && error !== null && "message" in error
    ? (error as { message?: unknown }).message
    : undefined;
  const raw = typeof detailMessage === "string"
    ? detailMessage.trim()
    : error instanceof Error
    ? error.message.trim()
    : String(error).trim();
  if (!raw || TECHNICAL_TERMS.some((pattern) => pattern.test(raw))) {
    return GENERIC_OPERATION_ERROR;
  }
  return raw;
}
