import type { DraftEditorSurface } from "./draft-editor-model";
import type { DraftEditorRequestPoint } from "./workflow-types";

const ELEMENT_NODE = 1;
const TEXT_NODE = 3;

export function endpointFromDom(
  node: Node | null,
  domOffset: number,
  surface: DraftEditorSurface,
): DraftEditorRequestPoint | null {
  if (node === null || !Number.isInteger(domOffset) || domOffset < 0) return null;
  const element = node.nodeType === ELEMENT_NODE
    ? node as Element
    : node.parentElement;
  const paragraph = element?.closest<HTMLElement>("[data-editor-paragraph]");
  if (!isEditorParagraph(paragraph, surface)) return null;
  if (paragraph.dataset.paragraphKind === "section_title") {
    return endpoint(paragraph.dataset.editorParagraph, 0);
  }

  const fragment = element?.closest<HTMLElement>("[data-text-fragment]");
  if (fragment !== null && fragment !== undefined) {
    const start = Number(fragment.dataset.utf16Start);
    const local = textOffsetWithinFragment(node, domOffset, fragment);
    if (Number.isInteger(start) && local !== null) {
      return endpoint(paragraph.dataset.editorParagraph, start + local);
    }
  }

  const editorText = element?.closest<HTMLElement>("[data-editor-text]");
  if (editorText !== null && editorText !== undefined && node === editorText) {
    return endpointForEditorTextBoundary(editorText, domOffset, paragraph);
  }
  if (node === paragraph) {
    return endpointForParagraphBoundary(paragraph, domOffset);
  }
  return null;
}

function isEditorParagraph(
  paragraph: HTMLElement | null | undefined,
  surface: DraftEditorSurface,
): paragraph is HTMLElement {
  return paragraph !== null
    && paragraph !== undefined
    && paragraph.dataset.surface === surface
    && paragraph.dataset.editorParagraph !== undefined;
}

function endpointForEditorTextBoundary(
  editorText: HTMLElement,
  domOffset: number,
  paragraph: HTMLElement,
): DraftEditorRequestPoint | null {
  const children = [...editorText.childNodes];
  if (domOffset > children.length) return null;
  const before = [...children.slice(0, domOffset)].reverse()
    .map(fragmentEnd)
    .find((offset): offset is number => offset !== null);
  const after = children.slice(domOffset)
    .map(fragmentStart)
    .find((offset): offset is number => offset !== null);
  const offset = before ?? after;
  return offset === undefined ? null : endpoint(paragraph.dataset.editorParagraph, offset);
}

function endpointForParagraphBoundary(
  paragraph: HTMLElement,
  domOffset: number,
): DraftEditorRequestPoint | null {
  const children = [...paragraph.childNodes];
  if (domOffset > children.length) return null;
  const editorTextIndex = children.findIndex(
    (child) => child.nodeType === ELEMENT_NODE
      && (child as HTMLElement).dataset.editorText === "true",
  );
  if (editorTextIndex < 0) return null;
  const editorText = children[editorTextIndex] as HTMLElement;
  return endpointForEditorTextBoundary(
    editorText,
    domOffset <= editorTextIndex ? 0 : editorText.childNodes.length,
    paragraph,
  );
}

function textOffsetWithinFragment(
  node: Node,
  domOffset: number,
  fragment: HTMLElement,
): number | null {
  if (node.nodeType === TEXT_NODE) {
    const length = node.textContent?.length ?? 0;
    return domOffset <= length ? domOffset : null;
  }
  if (node !== fragment || domOffset > fragment.childNodes.length) return null;
  return [...fragment.childNodes]
    .slice(0, domOffset)
    .reduce((total, child) => total + (child.textContent?.length ?? 0), 0);
}

function fragmentStart(node: Node): number | null {
  if (node.nodeType !== ELEMENT_NODE) return null;
  const element = node as HTMLElement;
  if (element.dataset.textFragment !== "true") return null;
  const start = Number(element.dataset.utf16Start);
  return Number.isInteger(start) ? start : null;
}

function fragmentEnd(node: Node): number | null {
  const start = fragmentStart(node);
  return start === null ? null : start + (node.textContent?.length ?? 0);
}

function endpoint(paragraphId: string | undefined, offset: number): DraftEditorRequestPoint | null {
  return paragraphId === undefined || !Number.isInteger(offset)
    ? null
    : { paragraph_id: paragraphId, offset, offset_encoding: "utf16" };
}
