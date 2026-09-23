import type {
  Clip,
  EditHistory,
  EditOperation,
  ReviewEditResponse,
  ReviewPayload,
} from "./types";

export function removeClip(clips: readonly Clip[], clipId: string): Clip[] {
  const next = clips.filter((clip) => clip.clip_id !== clipId);
  if (next.length === clips.length) {
    throw new Error("unknown clip");
  }
  if (next.length === 0) {
    throw new Error("proposal cannot be empty");
  }
  return next.map((clip) => ({ ...clip }));
}

export function moveClip(clips: readonly Clip[], from: number, to: number): Clip[] {
  if (from < 0 || from >= clips.length || to < 0 || to >= clips.length) {
    throw new Error("clip move is outside the sequence");
  }
  const next = clips.map((clip) => ({ ...clip }));
  const [selected] = next.splice(from, 1);
  if (selected === undefined) {
    throw new Error("clip move is invalid");
  }
  next.splice(to, 0, selected);
  return next;
}

export function clipsEqual(left: readonly Clip[], right: readonly Clip[]): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function deleteOperation(clipId: string): EditOperation {
  return { type: "delete", clip_id: clipId };
}

export function restoreOperation(
  clipId: string,
  insertBeforeClipId: string | null,
): EditOperation {
  return {
    type: "restore",
    clip_id: clipId,
    insert_before_clip_id: insertBeforeClipId,
  };
}

export function reorderOperation(
  clips: readonly Clip[],
  from: number,
  to: number,
  allowReorder: boolean,
): EditOperation {
  if (!allowReorder) {
    throw new Error("brief does not allow reorder");
  }
  return {
    type: "reorder",
    ordered_clip_ids: moveClip(clips, from, to).map((clip) => clip.clip_id),
  };
}

export function parseIntegerTicks(value: string): number {
  if (!/^-?\d+$/.test(value)) {
    throw new Error("trim bounds must use integer ticks");
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    throw new Error("trim bounds must use safe integer ticks");
  }
  return parsed;
}

export function trimOperation(
  clipId: string,
  sourceInTicks: string,
  sourceOutTicks: string,
): EditOperation {
  return {
    type: "trim",
    clip_id: clipId,
    source_in_ticks: parseIntegerTicks(sourceInTicks),
    source_out_ticks: parseIntegerTicks(sourceOutTicks),
  };
}

export class DecisionReviewState {
  private currentReview: ReviewPayload;
  private currentHistory: EditHistory;

  constructor(review: ReviewPayload, history: EditHistory) {
    validateDecisionState(review, history);
    this.currentReview = copied(review);
    this.currentHistory = copied(history);
  }

  get review(): ReviewPayload {
    return this.currentReview;
  }

  get history(): EditHistory {
    return this.currentHistory;
  }

  changePayload(operation: EditOperation): {
    active_edit_version_id: string;
    expected_revision: number;
    operation: EditOperation;
  } {
    return {
      ...this.navigationPayload(),
      operation: copied(operation),
    };
  }

  navigationPayload(): {
    active_edit_version_id: string;
    expected_revision: number;
  } {
    const activeId = this.currentHistory.active_edit_version_id;
    if (activeId === null) throw new Error("没有可调整的已确认剪辑版本");
    return {
      active_edit_version_id: activeId,
      expected_revision: this.currentHistory.project_revision,
    };
  }

  replace(response: ReviewEditResponse): void {
    validateDecisionState(response.review, response.edit_history);
    this.currentReview = copied(response.review);
    this.currentHistory = copied(response.edit_history);
  }
}

function validateDecisionState(review: ReviewPayload, history: EditHistory): void {
  if (review.basis.type !== "decision") {
    throw new Error("当前页面不是可调整的已确认剪辑");
  }
  if (
    history.active_edit_version_id !== review.basis.id
    || history.project_revision !== review.project.revision
  ) {
    throw new Error("已确认剪辑在读取期间发生了变化，请重新打开当前步骤");
  }
  if (
    history.active_schema_version !== review.schema_version
    || history.total_duration_ticks !== review.timeline.total_duration_ticks
    || !clipsEqual(history.current_clips, review.proposal.clips)
  ) {
    throw new Error("已确认剪辑内容与当前页面不一致，请重新打开当前步骤");
  }
}

function copied<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export class ReviewDraft {
  private baseline: Clip[];
  clips: Clip[];
  pendingProposalId: string | null;
  readonly allowReorder: boolean;
  projectRevision: number;
  confirmed = false;

  constructor(
    clips: readonly Clip[],
    projectRevision: number,
    allowReorder: boolean,
    pendingProposalId: string | null,
  ) {
    this.baseline = clips.map((clip) => ({ ...clip }));
    this.clips = clips.map((clip) => ({ ...clip }));
    this.projectRevision = projectRevision;
    this.allowReorder = allowReorder;
    this.pendingProposalId = pendingProposalId;
  }

  get dirty(): boolean {
    return !clipsEqual(this.baseline, this.clips);
  }

  delete(clipId: string): void {
    this.clips = removeClip(this.clips, clipId);
    this.pendingProposalId = null;
  }

  move(from: number, to: number): void {
    if (!this.allowReorder) {
      throw new Error("当前剪辑要求不允许调整同期声顺序");
    }
    this.clips = moveClip(this.clips, from, to);
    this.pendingProposalId = null;
  }

  proposalAccepted(proposalId: string, clips: readonly Clip[], projectRevision: number): void {
    if (projectRevision !== this.projectRevision) {
      throw new Error("保存待确认剪辑方案时项目内容意外变化，请重新打开当前步骤");
    }
    this.baseline = clips.map((clip) => ({ ...clip }));
    this.clips = clips.map((clip) => ({ ...clip }));
    this.pendingProposalId = proposalId;
  }

  confirmationPayload(): { proposal_id: string } {
    if (this.dirty || this.pendingProposalId === null) {
      throw new Error("请先保存并完整核对待确认剪辑方案，再进行确认");
    }
    return { proposal_id: this.pendingProposalId };
  }

  confirmedAt(projectRevision: number): void {
    if (projectRevision !== this.projectRevision + 1) {
      throw new Error("确认完成后的项目状态不一致，请重新打开当前步骤");
    }
    this.projectRevision = projectRevision;
    this.confirmed = true;
    this.pendingProposalId = null;
  }
}
