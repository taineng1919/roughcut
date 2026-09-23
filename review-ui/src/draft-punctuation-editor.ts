import { codePointToUtf16, utf16ToCodePoint } from "./draft-editor-model";

export type PunctuationInputMode = "typing" | "paste" | "composition";

export const PUNCTUATION_OVERFLOW_MESSAGE =
  "一个位置最多输入 8 个标点；多余内容未加入。";
export const PUNCTUATION_PASTE_OVERFLOW_MESSAGE =
  "一个位置最多输入 8 个标点；本次粘贴未加入。";
export const PUNCTUATION_NON_PUNCTUATION_MESSAGE =
  "正文原话不能直接改写；识别错误请校正原稿。";

export interface PunctuationValidationResult {
  accepted: boolean;
  value: string;
  message?: string;
}

export interface PunctuationSessionBinding {
  candidateId: string;
  checkpoint: { generation: number; checkpoint_hash: string };
  currentCandidate: {
    artifact_id: string;
    schema_version: number;
    content_hash: string;
  };
  paragraphId: string;
  blockId: string;
  startUtf16Offset: number;
  endUtf16Offset: number;
}

export interface PunctuationSessionPayload {
  paragraph_id: string;
  block_id: string;
  start_utf16_offset: number;
  end_utf16_offset: number;
  replacement: string;
}

export function isUnicodePunctuation(value: string): boolean {
  return /^\p{P}$/u.test(value);
}

export function punctuationStripped(value: string): string {
  return Array.from(value)
    .filter((character) => !isUnicodePunctuation(character))
    .join("");
}

export function validatePunctuationReplacement(
  text: string,
  startUtf16Offset: number,
  endUtf16Offset: number,
  replacement: string,
  mode: PunctuationInputMode,
): PunctuationValidationResult {
  let start: number;
  let end: number;
  try {
    start = utf16ToCodePoint(text, startUtf16Offset);
    end = utf16ToCodePoint(text, endUtf16Offset);
  } catch {
    return rejected(text, PUNCTUATION_NON_PUNCTUATION_MESSAGE);
  }
  if (end < start) return rejected(text, PUNCTUATION_NON_PUNCTUATION_MESSAGE);
  const replacementCharacters = Array.from(replacement);
  if (replacementCharacters.some((character) => !isUnicodePunctuation(character))) {
    return rejected(text, PUNCTUATION_NON_PUNCTUATION_MESSAGE);
  }
  const characters = Array.from(text);
  const next = [
    ...characters.slice(0, start),
    ...replacementCharacters,
    ...characters.slice(end),
  ].join("");
  if (next === text) return rejected(text);
  if (punctuationStripped(next) !== punctuationStripped(text)) {
    return rejected(text, PUNCTUATION_NON_PUNCTUATION_MESSAGE);
  }
  const changedEnd = start + replacementCharacters.length;
  for (const [runStart, runEnd] of punctuationRuns(next)) {
    if (
      runStart <= changedEnd
      && start <= runEnd
      && runEnd - runStart > 8
    ) {
      return rejected(
        text,
        mode === "paste"
          ? PUNCTUATION_PASTE_OVERFLOW_MESSAGE
          : PUNCTUATION_OVERFLOW_MESSAGE,
      );
    }
  }
  return { accepted: true, value: next };
}

export class PuncSession {
  private readonly originalText: string;
  private currentText: string;
  private dirty = false;

  constructor(
    binding: PunctuationSessionBinding,
    displayText: string,
  ) {
    this.b = {
      candidateId: binding.candidateId,
      checkpoint: { ...binding.checkpoint },
      currentCandidate: { ...binding.currentCandidate },
      paragraphId: binding.paragraphId,
      blockId: binding.blockId,
      startUtf16Offset: binding.startUtf16Offset,
      endUtf16Offset: binding.endUtf16Offset,
    };
    this.originalText = displayText;
    const start = utf16ToCodePoint(displayText, this.b.startUtf16Offset);
    const end = utf16ToCodePoint(displayText, this.b.endUtf16Offset);
    if (end < start) throw new Error("标点编辑范围无效");
    this.currentText = displayText;
  }

  readonly b: PunctuationSessionBinding;

  get value(): string {
    return this.currentText;
  }

  get changed(): boolean {
    return this.dirty;
  }

  apply(
    startUtf16Offset: number,
    endUtf16Offset: number,
    replacement: string,
    mode: PunctuationInputMode,
  ): PunctuationValidationResult {
    const result = validatePunctuationReplacement(
      this.currentText,
      startUtf16Offset,
      endUtf16Offset,
      replacement,
      mode,
    );
    if (!result.accepted) return result;
    this.currentText = result.value;
    this.dirty = this.currentText !== this.originalText;
    return result;
  }

  payload(): PunctuationSessionPayload | null {
    if (!this.dirty) return null;
    const before = Array.from(this.originalText);
    const after = Array.from(this.currentText);
    let start = 0;
    while (start < before.length && start < after.length && before[start] === after[start]) start += 1;
    let beforeEnd = before.length;
    let afterEnd = after.length;
    while (beforeEnd > start && afterEnd > start && before[beforeEnd - 1] === after[afterEnd - 1]) {
      beforeEnd -= 1;
      afterEnd -= 1;
    }
    return {
      paragraph_id: this.b.paragraphId,
      block_id: this.b.blockId,
      start_utf16_offset: codePointToUtf16(this.originalText, start),
      end_utf16_offset: codePointToUtf16(this.originalText, beforeEnd),
      replacement: after.slice(start, afterEnd).join(""),
    };
  }

  cancel(): void {
    this.currentText = this.originalText;
    this.dirty = false;
  }
}

function punctuationRuns(value: string): [number, number][] {
  const characters = Array.from(value);
  const runs: [number, number][] = [];
  let start: number | null = null;
  for (let index = 0; index < characters.length; index += 1) {
    if (isUnicodePunctuation(characters[index]!)) {
      start ??= index;
    } else if (start !== null) {
      runs.push([start, index]);
      start = null;
    }
  }
  if (start !== null) runs.push([start, characters.length]);
  return runs;
}

function rejected(text: string, message?: string): PunctuationValidationResult {
  return message === undefined
    ? { accepted: false, value: text }
    : { accepted: false, value: text, message };
}
