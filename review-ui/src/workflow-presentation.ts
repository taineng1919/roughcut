import type {
  ContentDraftBlock,
  ContentDraftRef,
  ReadableParagraph,
  WorkflowFilters,
} from "./workflow-types";

export interface ManuscriptParagraph {
  block: ContentDraftBlock;
  blockId: string;
  kind: "source_excerpt" | "narration";
  text: string;
}

export interface ManuscriptSection {
  key: string;
  title: string;
  paragraphs: ManuscriptParagraph[];
}

export interface BlockPlaybackTarget {
  sourceId: string;
  ticks: number;
  segmentId: string;
}

export interface ParagraphPlaybackTarget {
  sourceId: string;
  ticks: number;
  paragraphId: string;
}

export function draftManuscript(
  blocks: readonly ContentDraftBlock[],
  sourceName: (sourceId: string) => string,
): ManuscriptSection[] {
  const sections: ManuscriptSection[] = [];
  for (const block of blocks) {
    if (block.kind === "section_title") {
      sections.push({ key: `section:${block.block_id}`, title: block.title, paragraphs: [] });
      continue;
    }
    const sourceIds = block.kind === "source_excerpt"
      ? unique(block.refs.map((ref) => ref.source_id))
      : [];
    const key = block.kind === "narration" ? "narration" : `source:${sourceIds.join(":")}`;
    const title = block.kind === "narration"
      ? "解说"
      : sourceIds.map(sourceName).join(" / ");
    let section = sections.at(-1);
    if (section?.key !== key) {
      section = { key, title, paragraphs: [] };
      sections.push(section);
    }
    section.paragraphs.push({
      block,
      blockId: block.block_id,
      kind: block.kind,
      text: block.kind === "source_excerpt" ? block.canonical_text : block.text,
    });
  }
  return sections;
}

export function matchingParagraphIds(
  block: ContentDraftBlock,
  paragraphs: readonly ReadableParagraph[],
): string[] {
  const refs = blockRefs(block);
  const identities = new Set(refs.map(refIdentity));
  return paragraphs
    .filter((paragraph) => paragraph.refs.some((ref) => identities.has(refIdentity(ref))))
    .map((paragraph) => paragraph.paragraph_id);
}

export function playbackTargetForBlock(
  block: ContentDraftBlock,
): BlockPlaybackTarget | null {
  const ref = blockRefs(block)[0];
  return ref === undefined ? null : {
    sourceId: ref.source_id,
    ticks: ref.start_ticks,
    segmentId: ref.segment_id,
  };
}

export function playbackTargetForParagraph(
  paragraph: ReadableParagraph,
): ParagraphPlaybackTarget {
  return {
    sourceId: paragraph.source_id,
    ticks: paragraph.start_ticks,
    paragraphId: paragraph.paragraph_id,
  };
}

export function adoptionText(
  value: ReadableParagraph["adoption_status"],
): string {
  return {
    adopted: "已采用",
    partial: "部分采用",
    unadopted: "未采用",
    not_applicable: "暂无剪辑方案",
  }[value];
}

export function draftAdoptionForParagraph(
  paragraph: ReadableParagraph,
  blocks: readonly ContentDraftBlock[],
): ReadableParagraph["adoption_status"] {
  if (paragraph.adoption_status !== "not_applicable") return paragraph.adoption_status;
  const draftRefs = blocks.flatMap(blockRefs);
  let overlaps = false;
  const fullyCovered = paragraph.refs.every((paragraphRef) => {
    const matching = draftRefs.filter(
      (draftRef) => refIdentity(draftRef) === refIdentity(paragraphRef),
    );
    if (matching.some((draftRef) => rangesOverlap(draftRef, paragraphRef))) overlaps = true;
    return matching.some(
      (draftRef) => draftRef.start_ticks <= paragraphRef.start_ticks
        && draftRef.end_ticks >= paragraphRef.end_ticks,
    );
  });
  return fullyCovered ? "adopted" : overlaps ? "partial" : "unadopted";
}

export function filterParagraphsByDraftAdoption(
  paragraphs: readonly ReadableParagraph[],
  blocks: readonly ContentDraftBlock[],
  statuses: readonly string[],
): ReadableParagraph[] {
  if (statuses.length === 0) return [...paragraphs];
  const allowed = new Set(statuses);
  return paragraphs.filter((paragraph) => allowed.has(
    draftAdoptionForParagraph(paragraph, blocks),
  ));
}

export class TranscriptFilterState {
  private applied: WorkflowFilters = {};
  private pending = false;

  edit(filters: WorkflowFilters): void {
    this.pending = filterIdentity(filters) !== filterIdentity(this.applied);
  }

  apply(filters: WorkflowFilters): void {
    this.applied = copied(filters);
    this.pending = false;
  }

  clear(): void {
    this.applied = {};
    this.pending = false;
  }

  message(resultCount: number): string {
    if (this.pending) {
      return "筛选条件尚未应用；选择条件不会播放素材或修改初稿。";
    }
    if (Object.keys(this.applied).length > 0) {
      return `筛选已应用，当前显示 ${resultCount} 段原始转录稿。`;
    }
    return `未使用筛选，当前显示 ${resultCount} 段原始转录稿。`;
  }
}

function blockRefs(block: ContentDraftBlock): readonly ContentDraftRef[] {
  return block.kind === "source_excerpt" || block.kind === "narration"
    ? (block.kind === "source_excerpt" ? block.refs : block.recorded_refs)
    : [];
}

function refIdentity(ref: ContentDraftRef): string {
  return `${ref.source_id}:${ref.transcript_version_id}:${ref.segment_id}`;
}

function rangesOverlap(left: ContentDraftRef, right: ContentDraftRef): boolean {
  return left.start_ticks < right.end_ticks && right.start_ticks < left.end_ticks;
}

function filterIdentity(filters: WorkflowFilters): string {
  return JSON.stringify({
    source_ids: filters.source_ids ?? [],
    person_ids: filters.person_ids ?? [],
    adoption_statuses: filters.adoption_statuses ?? [],
    keyword: filters.keyword ?? "",
  });
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

function copied<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}
