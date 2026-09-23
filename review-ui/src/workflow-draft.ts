import type {
  ContentDraftBlock,
  ContentDraftRef,
  NarrationBlock,
  SelectionResolution,
  SourceExcerptBlock,
  WorkflowReviewPayload,
} from "./workflow-types";

interface DeletedBlock {
  block: ContentDraftBlock;
  index: number;
}

export class WorkflowDraftEditor {
  private baseline: ContentDraftBlock[];
  private deleted: DeletedBlock[] = [];
  private currentBlocks: ContentDraftBlock[];
  private displayNumbers = new Map<string, string>();
  private nextDisplayNumber: number;
  readonly allowReorder: boolean;
  readonly parentDraftId: string | null;

  constructor(
    blocks: readonly ContentDraftBlock[],
    allowReorder: boolean,
    parentDraftId: string | null,
  ) {
    this.baseline = copiedBlocks(blocks);
    this.currentBlocks = copiedBlocks(blocks);
    this.allowReorder = allowReorder;
    this.parentDraftId = parentDraftId;
    blocks.forEach((block, index) => {
      this.displayNumbers.set(block.block_id, `D${String(index + 1).padStart(3, "0")}`);
    });
    this.nextDisplayNumber = blocks.length + 1;
  }

  get blocks(): ContentDraftBlock[] {
    return copied(this.currentBlocks);
  }

  get dirty(): boolean {
    return JSON.stringify(this.baseline) !== JSON.stringify(this.currentBlocks);
  }

  get canRestore(): boolean {
    return this.deleted.length > 0;
  }

  get hasUnrecordedNarration(): boolean {
    return this.currentBlocks.some(
      (block) => block.kind === "narration" && block.status !== "recorded",
    );
  }

  displayNumber(blockId: string): string {
    const number = this.displayNumbers.get(blockId);
    if (number === undefined) throw new Error("未知稿本块");
    return number;
  }

  delete(blockId: string): void {
    const index = this.currentBlocks.findIndex((block) => block.block_id === blockId);
    if (index < 0) throw new Error("未知稿本块");
    if (this.currentBlocks.length === 1) throw new Error("稿本至少保留一个块");
    const [block] = this.currentBlocks.splice(index, 1);
    if (block === undefined) throw new Error("未知稿本块");
    this.deleted.push({ block, index });
  }

  restoreLastDeleted(): boolean {
    const deleted = this.deleted.pop();
    if (deleted === undefined) return false;
    const index = Math.min(deleted.index, this.currentBlocks.length);
    this.currentBlocks.splice(index, 0, deleted.block);
    return true;
  }

  move(from: number, to: number): void {
    if (!this.allowReorder) throw new Error("当前剪辑要求需要尽量保持原顺序");
    if (
      from < 0
      || from >= this.currentBlocks.length
      || to < 0
      || to >= this.currentBlocks.length
    ) {
      throw new Error("稿本移动位置无效");
    }
    const [block] = this.currentBlocks.splice(from, 1);
    if (block === undefined) throw new Error("稿本移动位置无效");
    this.currentBlocks.splice(to, 0, block);
  }

  editNarration(blockId: string, text: string): void {
    const index = this.currentBlocks.findIndex((block) => block.block_id === blockId);
    const block = this.currentBlocks[index];
    if (block === undefined) throw new Error("未知稿本块");
    if (block.kind !== "narration") throw new Error("来源原话只读");
    const normalized = text.trim();
    if (!normalized) throw new Error("解说文字不能为空");
    if (normalized === block.text) return;
    this.currentBlocks[index] = {
      ...block,
      text: normalized,
      status: "draft",
      recorded_refs: [],
    };
  }

  addNarration(blockId: string, text: string, index: number): void {
    this.validateInsert(blockId, index);
    const normalized = text.trim();
    if (!normalized) throw new Error("解说文字不能为空");
    const block: NarrationBlock = {
      block_id: blockId,
      kind: "narration",
      text: normalized,
      status: "draft",
      recorded_refs: [],
    };
    this.assignDisplayNumber(blockId);
    this.currentBlocks.splice(index, 0, block);
  }

  insertResolution(
    blockId: string,
    resolution: SelectionResolution,
    index: number,
  ): void {
    this.validateInsert(blockId, index);
    if (!resolution.refs.length || !resolution.canonical_text.trim()) {
      throw new Error("选区没有可加入的精确引用");
    }
    const block: SourceExcerptBlock = {
      block_id: blockId,
      kind: "source_excerpt",
      refs: resolution.refs.map(refWithoutText),
      canonical_text: resolution.canonical_text,
    };
    this.assignDisplayNumber(blockId);
    this.currentBlocks.splice(index, 0, block);
  }

  candidatePayload(): {
    parent_draft_id: string | null;
    blocks: ContentDraftBlock[];
  } {
    if (!this.currentBlocks.length) throw new Error("稿本不能为空");
    return {
      parent_draft_id: this.parentDraftId,
      blocks: copied(this.currentBlocks),
    };
  }

  private validateInsert(blockId: string, index: number): void {
    if (!/^[A-Za-z0-9_-]+$/.test(blockId)) throw new Error("稿本块 ID 无效");
    if (this.currentBlocks.some((block) => block.block_id === blockId)) {
      throw new Error("稿本块 ID 重复");
    }
    if (index < 0 || index > this.currentBlocks.length) {
      throw new Error("稿本插入位置无效");
    }
  }

  private assignDisplayNumber(blockId: string): void {
    this.displayNumbers.set(
      blockId,
      `D${String(this.nextDisplayNumber).padStart(3, "0")}`,
    );
    this.nextDisplayNumber += 1;
  }
}

export function draftActionState(
  editor: WorkflowDraftEditor,
  snapshot: WorkflowReviewPayload,
  busy = false,
): {
  canSaveCandidate: boolean;
  canConfirmDraft: boolean;
  showProposalAction: false;
} {
  const locked = snapshot.session.read_only || busy;
  const draft = snapshot.content_draft?.content_draft;
  return {
    canSaveCandidate: !locked
      && editor.dirty
      && editor.blocks.length > 0
      && snapshot.allowed_operations.includes("content_draft_create"),
    canConfirmDraft: !locked
      && !editor.dirty
      && draft !== undefined
      && !draft.confirmed_by_user
      && snapshot.allowed_operations.includes("content_draft_confirm"),
    showProposalAction: false,
  };
}

function refWithoutText(ref: ContentDraftRef & { canonical_text?: string }): ContentDraftRef {
  return {
    source_id: ref.source_id,
    transcript_version_id: ref.transcript_version_id,
    segment_id: ref.segment_id,
    start_ticks: ref.start_ticks,
    end_ticks: ref.end_ticks,
  };
}

function copied<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function copiedBlocks(value: readonly ContentDraftBlock[]): ContentDraftBlock[] {
  return JSON.parse(JSON.stringify(value)) as ContentDraftBlock[];
}
