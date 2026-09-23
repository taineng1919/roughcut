export interface SelectionPayload {
  paragraph_id: string;
  start_offset: number;
  end_offset: number;
  quote: string;
  occurrence: number;
}

export class SelectionPayloadCache {
  private value: SelectionPayload | null = null;

  capture(payload: SelectionPayload): void {
    this.value = { ...payload };
  }

  consume(paragraphId: string): SelectionPayload | null {
    const payload = this.value;
    this.value = null;
    if (payload !== null && payload.paragraph_id !== paragraphId) {
      throw new Error("缓存选区不属于当前原始转录段落");
    }
    return payload === null ? null : { ...payload };
  }

  clear(): void {
    this.value = null;
  }
}

export function selectionResolutionAction(
  mode: "exact" | "expanded_to_segments",
): "insert" | "confirm_expansion" {
  return mode === "exact" ? "insert" : "confirm_expansion";
}

export function validateSelectionParagraph(
  startParagraphId: string | null,
  endParagraphId: string | null,
): string {
  if (
    startParagraphId === null
    || endParagraphId === null
    || startParagraphId !== endParagraphId
  ) {
    throw new Error("选区必须位于同一个原始转录段落内");
  }
  return startParagraphId;
}

export function utf16OffsetToCodePoint(text: string, offset: number): number {
  if (!Number.isInteger(offset) || offset < 0 || offset > text.length) {
    throw new Error("选区偏移超出段落范围");
  }
  if (
    offset > 0
    && offset < text.length
    && isHighSurrogate(text.charCodeAt(offset - 1))
    && isLowSurrogate(text.charCodeAt(offset))
  ) {
    throw new Error("选区边界不能落在 UTF-16 代理对中间");
  }
  return Array.from(text.slice(0, offset)).length;
}

export function selectionPayloadFromUtf16(
  paragraphId: string,
  text: string,
  startUtf16: number,
  endUtf16: number,
): SelectionPayload {
  if (!paragraphId) throw new Error("选区缺少 paragraph_id");
  if (endUtf16 <= startUtf16) throw new Error("请选择正向非空文字范围");
  const start = utf16OffsetToCodePoint(text, startUtf16);
  const end = utf16OffsetToCodePoint(text, endUtf16);
  const quote = text.slice(startUtf16, endUtf16);
  if (!quote) throw new Error("请选择正向非空文字范围");
  const occurrences = codePointOccurrences(text, quote);
  const occurrence = occurrences.indexOf(start);
  if (occurrence < 0) throw new Error("选区文字与段落偏移不一致");
  return {
    paragraph_id: paragraphId,
    start_offset: start,
    end_offset: end,
    quote,
    occurrence,
  };
}

function codePointOccurrences(text: string, quote: string): number[] {
  const haystack = Array.from(text);
  const needle = Array.from(quote);
  const result: number[] = [];
  for (let index = 0; index <= haystack.length - needle.length; index += 1) {
    if (needle.every((value, offset) => haystack[index + offset] === value)) {
      result.push(index);
    }
  }
  return result;
}

function isHighSurrogate(value: number): boolean {
  return 0xD800 <= value && value <= 0xDBFF;
}

function isLowSurrogate(value: number): boolean {
  return 0xDC00 <= value && value <= 0xDFFF;
}
