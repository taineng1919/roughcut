import type {
  DraftEditorSnapshot,
  ReadableParagraph,
  ReadableTranscriptPage,
  WorkflowApi,
  WorkflowReviewPayload,
} from "./workflow-types";

export async function initialDraftEditorSnapshot(
  api: WorkflowApi,
): Promise<DraftEditorSnapshot | null> {
  try {
    const snapshot = await api<DraftEditorSnapshot>(
      "/api/workflow/draft-editor",
    );
    return snapshot.review_mode === "draft_editor" ? snapshot : null;
  } catch {
    return null;
  }
}

export function reviewMode(value: unknown): "workflow" | "artifact" {
  if (
    typeof value === "object"
    && value !== null
    && "review_mode" in value
    && value.review_mode === "workflow"
    && "workflow_schema_version" in value
    && value.workflow_schema_version === 1
  ) {
    return "workflow";
  }
  return "artifact";
}

export function workflowWritesLocked(
  snapshot: WorkflowReviewPayload,
  requestPending: boolean,
): boolean {
  return snapshot.session.read_only || requestPending;
}

export function workflowSessionMessage(snapshot: WorkflowReviewPayload): string {
  const session = snapshot.session;
  if (!session.read_only) {
    return "当前页面内容可正常使用。";
  }
  return "项目内容已在别处更新，本页已转为只读，请重新打开当前步骤。";
}

export function workflowBindingLabels(snapshot: WorkflowReviewPayload): string[] {
  return snapshot.source_bindings.map((binding, index) => {
    const source = snapshot.sources.find((item) => item.source_id === binding.source_id);
    return `${index + 1}. ${source?.display_name ?? binding.source_id} · ${binding.source_id} / ${binding.transcript_version_id}`;
  });
}

export class ReadableTranscriptAccumulator {
  private currentHash: string | null = null;
  private expectedOffset = 0;
  private finished = false;
  private items: ReadableParagraph[] = [];
  private identity = new Set<string>();
  private bindingIdentity: string | null = null;
  private revision: number | null = null;
  private expectedTotal: number | null = null;

  get paragraphs(): ReadableParagraph[] {
    return this.items.map((paragraph) => copied(paragraph));
  }

  get viewHash(): string | null {
    return this.currentHash;
  }

  get nextOffset(): number {
    return this.expectedOffset;
  }

  get total(): number {
    return this.expectedTotal ?? 0;
  }

  get complete(): boolean {
    return this.finished;
  }

  append(page: ReadableTranscriptPage): void {
    const bindings = JSON.stringify(page.source_bindings);
    if (this.currentHash === null) {
      if (page.offset !== 0) throw new Error("文稿分页必须从第一页开始");
      this.currentHash = page.view_hash;
      this.bindingIdentity = bindings;
      this.revision = page.project_revision;
      this.expectedTotal = page.total;
    } else if (
      page.view_hash !== this.currentHash
      || bindings !== this.bindingIdentity
      || page.project_revision !== this.revision
      || page.total !== this.expectedTotal
    ) {
      throw new Error("原始转录稿在分页读取期间发生了变化，请重新打开当前步骤");
    }
    if (this.finished || page.offset !== this.expectedOffset) {
      throw new Error("文稿分页顺序不连续");
    }
    for (const paragraph of page.paragraphs) {
      if (this.identity.has(paragraph.paragraph_id)) {
        throw new Error("文稿分页包含重复 paragraph identity");
      }
      this.identity.add(paragraph.paragraph_id);
      this.items.push(copied(paragraph));
    }
    const naturalNext = page.offset + page.paragraphs.length;
    if (page.next_cursor !== null && page.next_cursor !== naturalNext) {
      throw new Error("文稿 next cursor 不连续");
    }
    this.expectedOffset = page.next_cursor ?? naturalNext;
    this.finished = page.next_cursor === null;
    if (this.finished && this.items.length !== this.expectedTotal) {
      throw new Error("文稿分页结束但未覆盖完整结果");
    }
  }
}

export class WorkflowPanelState {
  private selected: "draft" | "transcript" | "player" = "draft";

  get active(): "draft" | "transcript" | "player" {
    return this.selected;
  }

  select(value: "draft" | "transcript" | "player"): void {
    this.selected = value;
  }
}

function copied<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}
