import { utf16ToCodePoint } from "./draft-editor-model";
import type { DraftEditorSelectionRequest } from "./workflow-types";

interface VisibleParagraph {
  paragraph_id: string;
  text: string;
}

export function visibleSelectionQuote(
  paragraphs: readonly VisibleParagraph[],
  selection: DraftEditorSelectionRequest,
): string | null {
  const anchorIndex = paragraphs.findIndex(
    (paragraph) => paragraph.paragraph_id === selection.anchor.paragraph_id,
  );
  const focusIndex = paragraphs.findIndex(
    (paragraph) => paragraph.paragraph_id === selection.focus.paragraph_id,
  );
  if (anchorIndex < 0 || focusIndex < 0) return null;
  try {
    const anchor = utf16ToCodePoint(
      paragraphs[anchorIndex]!.text,
      selection.anchor.offset,
    );
    const focus = utf16ToCodePoint(
      paragraphs[focusIndex]!.text,
      selection.focus.offset,
    );
    const forward = anchorIndex < focusIndex
      || (anchorIndex === focusIndex && anchor <= focus);
    const startIndex = forward ? anchorIndex : focusIndex;
    const endIndex = forward ? focusIndex : anchorIndex;
    const start = forward ? anchor : focus;
    const end = forward ? focus : anchor;
    return paragraphs.slice(startIndex, endIndex + 1).map((paragraph, index, range) => {
      const characters = Array.from(paragraph.text);
      if (range.length === 1) return characters.slice(start, end).join("");
      if (index === 0) return characters.slice(start).join("");
      if (index === range.length - 1) return characters.slice(0, end).join("");
      return paragraph.text;
    }).join("\n");
  } catch {
    return null;
  }
}
