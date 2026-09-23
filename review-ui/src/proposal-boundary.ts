import type { ReviewPayload } from "./types";
import type {
  ReadableParagraph,
  ReadableTranscriptPage,
  SourceBinding,
  WorkflowApi,
} from "./workflow-types";

export interface ProposalBoundarySummary {
  basisType: "proposal" | "decision";
  artifactId: string;
  proposalId: string;
  contentDraftId: string | null;
  projectRevision: number;
  schemaVersion: 1 | 2;
  clipCount: number;
  totalDurationTicks: number;
  bindings: SourceBinding[];
  renderAuthorized: false;
}

export interface ProposalDiffPayload {
  applicable: boolean;
  reason?: string;
  project_revision: number;
  proposal_diff?: {
    base_edit_version_id: string;
    proposal_id: string;
    schema_version: 1 | 2;
    before_clip_count: number;
    after_clip_count: number;
    before_duration_ticks: number;
    after_duration_ticks: number;
    duration_delta_ticks: number;
    added: Array<Record<string, unknown>>;
    removed: Array<Record<string, unknown>>;
    changed: Array<Record<string, unknown>>;
    order_changed: boolean;
    before_order: string[];
    after_order: string[];
  };
}

export type ProposalDiffSummary =
  | { kind: "none"; label: string }
  | {
    kind: "diff";
    baseEditVersionId: string;
    addedCount: number;
    removedCount: number;
    changedCount: number;
    orderChanged: boolean;
    durationDeltaTicks: number;
    payload: NonNullable<ProposalDiffPayload["proposal_diff"]>;
  };

export interface ProposalCoverageSummary {
  adopted: number;
  partial: number;
  unadopted: number;
  total: number;
}

export interface ProposalCoverageResult {
  artifact: {
    basis: "proposal";
    artifact_id: string;
    content_draft_id: string | null;
  };
  paragraphs: ReadableParagraph[];
  viewHash: string;
}

interface ProposalCoveragePage {
  artifact: ProposalCoverageResult["artifact"];
  readable_transcript: ReadableTranscriptPage;
}

export function proposalBoundarySummary(
  review: ReviewPayload,
  contentDraftId: string | null,
): ProposalBoundarySummary {
  if (review.proposal.proposal_id === "" || review.basis.id === "") {
    throw new Error("审阅内容缺少必要的身份信息");
  }
  return {
    basisType: review.basis.type,
    artifactId: review.basis.id,
    proposalId: review.proposal.proposal_id,
    contentDraftId,
    projectRevision: review.project.revision,
    schemaVersion: review.schema_version,
    clipCount: review.proposal.clips.length,
    totalDurationTicks: review.proposal.total_duration_ticks,
    bindings: review.sources.map((source) => ({
      source_id: source.source_id,
      transcript_version_id: source.transcript_version_id,
    })),
    renderAuthorized: false,
  };
}

export function proposalDiffSummary(payload: ProposalDiffPayload): ProposalDiffSummary {
  if (!payload.applicable) {
    return {
      kind: "none",
      label: payload.reason === "no_active_decision"
        ? "首次方案，无上一版可比较"
        : "没有可比较的上一版",
    };
  }
  const diff = payload.proposal_diff;
  if (diff === undefined) throw new Error("无法读取剪辑方案与上一版的变化");
  return {
    kind: "diff",
    baseEditVersionId: diff.base_edit_version_id,
    addedCount: diff.added.length,
    removedCount: diff.removed.length,
    changedCount: diff.changed.length,
    orderChanged: diff.order_changed,
    durationDeltaTicks: diff.duration_delta_ticks,
    payload: diff,
  };
}

export function proposalConfirmationReady(
  basisType: "proposal" | "decision",
  evidenceLoaded: boolean,
): boolean {
  return basisType === "proposal" && evidenceLoaded;
}

export function coverageSummary(
  paragraphs: ReadableParagraph[],
  complete: boolean,
): ProposalCoverageSummary {
  if (!complete) throw new Error("剪辑方案引用的原始转录稿尚未完整加载");
  const result: ProposalCoverageSummary = {
    adopted: 0,
    partial: 0,
    unadopted: 0,
    total: paragraphs.length,
  };
  for (const paragraph of paragraphs) {
    if (paragraph.adoption_status === "adopted") result.adopted += 1;
    else if (paragraph.adoption_status === "partial") result.partial += 1;
    else if (paragraph.adoption_status === "unadopted") result.unadopted += 1;
    else throw new Error("剪辑方案返回了无法识别的采用情况");
  }
  return result;
}

export async function loadProposalCoverage(
  api: WorkflowApi,
  proposalId: string,
  pageLimit = 100,
): Promise<ProposalCoverageResult> {
  const paragraphs: ReadableParagraph[] = [];
  let offset = 0;
  let expectedHash: string | null = null;
  let expectedTotal: number | null = null;
  let artifact: ProposalCoverageResult["artifact"] | null = null;
  while (true) {
    const response = await api<ProposalCoveragePage>("/api/proposal-coverage", {
      method: "POST",
      body: JSON.stringify({ offset, limit: pageLimit }),
    });
    const page = response.readable_transcript;
    if (response.artifact.basis !== "proposal" || response.artifact.artifact_id !== proposalId) {
      throw new Error("正在读取的剪辑方案与当前页面不一致");
    }
    if (artifact === null) artifact = { ...response.artifact };
    else if (JSON.stringify(artifact) !== JSON.stringify(response.artifact)) {
      throw new Error("剪辑方案在读取期间发生了变化");
    }
    if (expectedHash === null) {
      expectedHash = page.view_hash;
      expectedTotal = page.total;
    } else if (page.view_hash !== expectedHash || page.total !== expectedTotal) {
      throw new Error("原始转录稿在分页读取期间发生了变化");
    }
    if (page.offset !== offset) throw new Error("原始转录稿分页不连续");
    paragraphs.push(...page.paragraphs);
    const next = page.next_cursor;
    if (next === null) {
      if (paragraphs.length !== expectedTotal) {
        throw new Error("没有完整加载剪辑方案引用的原始转录稿");
      }
      if (artifact === null || expectedHash === null) {
        throw new Error("剪辑方案没有返回可审阅内容");
      }
      return { artifact, paragraphs, viewHash: expectedHash };
    }
    if (next !== offset + page.paragraphs.length) {
      throw new Error("原始转录稿分页位置不连续");
    }
    offset = next;
  }
}

export class ProposalBoundaryController {
  private generation = 0;

  constructor(private readonly api: WorkflowApi) {}

  async render(review: ReviewPayload, contentDraftId: string | null): Promise<boolean> {
    const generation = ++this.generation;
    const summary = proposalBoundarySummary(review, contentDraftId);
    element<HTMLElement>("artifact-boundary").hidden = false;
    element<HTMLElement>("artifact-boundary-title").textContent = summary.basisType === "proposal"
      ? "确认剪辑方案"
      : "调整已确认剪辑";
    element<HTMLElement>("artifact-boundary-identity").textContent = [
      `${summary.basisType === "proposal" ? "Proposal" : "Decision"} ${summary.artifactId}`,
      `Proposal ${summary.proposalId}`,
      `revision ${summary.projectRevision}`,
      `schema ${summary.schemaVersion}`,
      `${summary.clipCount} clips`,
      formatTicks(summary.totalDurationTicks),
    ].join(" · ");
    element<HTMLOListElement>("artifact-boundary-bindings").replaceChildren(
      ...summary.bindings.map((binding, index) => listItem(
        `${index + 1}. ${binding.source_id} / ${binding.transcript_version_id}`,
      )),
    );
    const origin = element<HTMLElement>("artifact-boundary-origin");
    origin.textContent = summary.contentDraftId === null
      ? "Content Draft 来源：当前启动方式未提供可验证的 draft ID。"
      : `Content Draft 来源：${summary.contentDraftId}`;
    const proposalOnly = summary.basisType === "proposal";
    element<HTMLElement>("proposal-evidence").hidden = !proposalOnly;
    element<HTMLElement>("decision-boundary").hidden = proposalOnly;
    if (!proposalOnly) {
      element<HTMLElement>("decision-boundary").textContent =
        "这里可以删除、恢复、调整顺序和微调已确认剪辑。每次成功操作都会保留为新的不可变版本；本页面不会开始正式导出。";
      return false;
    }

    const coverageStatus = element<HTMLElement>("proposal-coverage-status");
    const coverageList = element<HTMLElement>("proposal-coverage-list");
    const diffStatus = element<HTMLElement>("proposal-diff-status");
    const diffDetail = element<HTMLElement>("proposal-diff-detail");
    coverageStatus.textContent = "正在读取完整原始转录稿…";
    coverageList.replaceChildren();
    diffStatus.textContent = "正在读取与已确认版本的变化…";
    diffDetail.replaceChildren();

    const [coverage, diffPayload] = await Promise.all([
      loadProposalCoverage(this.api, summary.proposalId),
      this.api<ProposalDiffPayload>("/api/proposal-diff"),
    ]);
    if (generation !== this.generation) return false;
    if (
      summary.contentDraftId !== null
      && coverage.artifact.content_draft_id !== summary.contentDraftId
    ) {
      throw new Error("剪辑方案引用的初稿与当前页面不一致");
    }
    if (summary.contentDraftId === null && coverage.artifact.content_draft_id !== null) {
      origin.textContent = `Content Draft 来源：${coverage.artifact.content_draft_id}`;
    }
    const counts = coverageSummary(coverage.paragraphs, true);
    coverageStatus.textContent =
      `已完整加载 ${counts.total} 段：已采用 ${counts.adopted} 段，部分采用 ${counts.partial} 段，未采用 ${counts.unadopted} 段。`;
    coverageList.replaceChildren(...coverage.paragraphs.map((paragraph) => {
      const item = document.createElement("article");
      item.className = `proposal-coverage-item adoption-${paragraph.adoption_status}`;
      const heading = document.createElement("strong");
      heading.textContent = `${paragraph.source_display_name} · ${paragraph.person_name ?? "人物未对应"} · ${adoptionLabel(paragraph.adoption_status)}`;
      const copy = document.createElement("p");
      copy.textContent = paragraph.text;
      const time = document.createElement("small");
      time.textContent = `${formatTicks(paragraph.start_ticks)}–${formatTicks(paragraph.end_ticks)}`;
      const technical = jsonDetails("技术详情", { paragraph_id: paragraph.paragraph_id });
      item.append(heading, copy, time, technical);
      return item;
    }));

    const diff = proposalDiffSummary(diffPayload);
    if (diff.kind === "none") {
      diffStatus.textContent = diff.label;
      return true;
    }
    diffStatus.textContent = [
      `新增 ${diff.addedCount} 段`,
      `删除 ${diff.removedCount} 段`,
      `修改 ${diff.changedCount} 段`,
      `顺序${diff.orderChanged ? "有变化" : "未变化"}`,
      `时长变化 ${formatSignedTicks(diff.durationDeltaTicks)}`,
    ].join(" · ");
    diffDetail.append(
      jsonDetails("新增内容", diff.payload.added),
      jsonDetails("删除内容", diff.payload.removed),
      jsonDetails("修改内容", diff.payload.changed),
      jsonDetails("调整前后顺序", {
        before: diff.payload.before_order,
        after: diff.payload.after_order,
      }),
    );
    return true;
  }
}

function jsonDetails(label: string, value: unknown): HTMLDetailsElement {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = label;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(value, null, 2);
  details.append(summary, pre);
  return details;
}

function listItem(text: string): HTMLLIElement {
  const item = document.createElement("li");
  item.textContent = text;
  return item;
}

function adoptionLabel(value: ReadableParagraph["adoption_status"]): string {
  return {
    adopted: "已采用",
    partial: "部分采用",
    unadopted: "未采用",
    not_applicable: "暂无剪辑方案",
  }[value];
}

function formatTicks(ticks: number): string {
  const milliseconds = Math.floor((ticks * 1000) / 120_000);
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.floor((milliseconds % 60_000) / 1000);
  const millis = milliseconds % 1000;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function formatSignedTicks(ticks: number): string {
  return `${ticks >= 0 ? "+" : "−"}${formatTicks(Math.abs(ticks))}`;
}

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (found === null) throw new Error(`missing #${id}`);
  return found as T;
}
