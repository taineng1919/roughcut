import type { ReviewPayload } from "./types";
import type {
  DraftWorkspaceMutationBasis,
  WorkflowApi,
  WorkflowReviewPayload,
} from "./workflow-types";

export interface WorkflowProposalEligibility {
  visible: boolean;
  contentDraftId: string | null;
}

export interface WorkflowProposalHandoff {
  contentDraftId: string;
  review: ReviewPayload;
}

export interface ConfirmedContentDraft {
  contentDraftId: string;
  projectRevision: number;
}

interface ContentDraftConfirmationResponse {
  content_draft_mutation: {
    content_draft: {
      content_draft_id: string;
      parent_draft_id: string | null;
      confirmed_by_user: boolean;
    };
    project_revision: number;
  };
  workflow: {
    project: { revision: number };
    content_draft: {
      content_draft: {
        content_draft_id: string;
        confirmed_by_user: boolean;
      };
    } | null;
  };
}

interface WorkflowProposalResponse {
  content_draft_proposal: {
    content_draft_id: string;
    proposal_schema_version: 1 | 2;
    proposal: ReviewPayload["proposal"];
    project_revision: number;
  };
  review: ReviewPayload;
}

export async function performWorkflowProposalHandoff(
  api: WorkflowApi,
  displayedContentDraftId: string,
  alreadyConfirmed: boolean,
  confirmationRequest:
    & DraftWorkspaceMutationBasis
    & { operation_id: string; content_draft_id: string },
  onConfirmed: (
    confirmedContentDraftId: string,
    newlyConfirmed: boolean,
  ) => Promise<void>,
  onHandoff: (handoff: WorkflowProposalHandoff) => Promise<void>,
): Promise<void> {
  let confirmedContentDraftId = displayedContentDraftId;
  let confirmedProjectRevision: number | null = null;
  if (!alreadyConfirmed) {
    const confirmation = await api(
      "/api/workflow/content-draft-confirm",
      post(confirmationRequest),
    );
    const confirmed = parseConfirmedContentDraft(
      confirmation,
      displayedContentDraftId,
    );
    confirmedContentDraftId = confirmed.contentDraftId;
    confirmedProjectRevision = confirmed.projectRevision;
  }
  await onConfirmed(confirmedContentDraftId, !alreadyConfirmed);
  const proposal = await api(
    "/api/workflow/content-draft-propose",
    post({ content_draft_id: confirmedContentDraftId }),
  );
  const handoff = parseWorkflowProposalHandoff(proposal, confirmedContentDraftId);
  if (
    confirmedProjectRevision !== null
    && handoff.review.project.revision !== confirmedProjectRevision
  ) {
    throw new Error("粗剪预览没有基于刚确认的初稿版本生成");
  }
  await onHandoff(handoff);
}

export function parseConfirmedContentDraft(
  value: unknown,
  requestedContentDraftId: string,
): ConfirmedContentDraft {
  const response = value as ContentDraftConfirmationResponse;
  const mutation = response?.content_draft_mutation;
  const confirmed = mutation?.content_draft;
  const selected = response?.workflow?.content_draft?.content_draft;
  if (
    confirmed?.parent_draft_id !== requestedContentDraftId
    || confirmed.confirmed_by_user !== true
  ) {
    throw new Error("初稿确认结果没有引用当前页面的精确初稿");
  }
  if (
    selected?.content_draft_id !== confirmed.content_draft_id
    || selected.confirmed_by_user !== true
  ) {
    throw new Error("初稿确认结果与当前工作流不一致");
  }
  if (
    !Number.isInteger(mutation.project_revision)
    || response.workflow.project.revision !== mutation.project_revision
  ) {
    throw new Error("初稿确认期间项目内容发生了变化");
  }
  return {
    contentDraftId: confirmed.content_draft_id,
    projectRevision: mutation.project_revision,
  };
}

export function workflowProposalEligibility(
  snapshot: WorkflowReviewPayload,
  draftDirty: boolean,
  requestInFlight: boolean,
): WorkflowProposalEligibility {
  const state = snapshot.content_draft;
  const draft = state?.content_draft;
  const bindingsMatch = draft !== undefined
    && JSON.stringify(draft.source_bindings) === JSON.stringify(snapshot.source_bindings);
  const briefMatches = draft !== undefined
    && snapshot.brief !== null
    && JSON.stringify(draft.brief_snapshot) === JSON.stringify(snapshot.brief);
  const narrationRecorded = draft?.blocks.every(
    (block) => block.kind !== "narration" || block.status === "recorded",
  ) ?? false;
  const visible = !draftDirty
    && !requestInFlight
    && !snapshot.session.read_only
    && snapshot.session.status === "current"
    && state?.status === "current"
    && draft?.confirmed_by_user === true
    && draft.base_project_revision === snapshot.project.revision
    && draft.context_hash === snapshot.context_hash
    && bindingsMatch
    && briefMatches
    && narrationRecorded
    && snapshot.allowed_operations.includes("content_draft_propose");
  return {
    visible,
    contentDraftId: visible ? draft?.content_draft_id ?? null : null,
  };
}

export function parseWorkflowProposalHandoff(
  value: unknown,
  displayedContentDraftId: string,
): WorkflowProposalHandoff {
  const response = value as WorkflowProposalResponse;
  const compiled = response?.content_draft_proposal;
  const review = response?.review;
  if (compiled?.content_draft_id !== displayedContentDraftId) {
    throw new Error("待确认剪辑方案引用的初稿与当前页面不一致");
  }
  if (review?.basis?.type !== "proposal") {
    throw new Error("生成结果不是可审阅的待确认剪辑方案");
  }
  if (
    compiled.proposal.proposal_id !== review.basis.id
    || review.proposal.proposal_id !== review.basis.id
  ) {
    throw new Error("待确认剪辑方案的身份信息不一致");
  }
  if (
    compiled.proposal_schema_version !== review.schema_version
    || compiled.proposal.schema_version !== review.schema_version
    || review.proposal.schema_version !== review.schema_version
  ) {
    throw new Error("待确认剪辑方案的数据版本不一致");
  }
  if (
    compiled.project_revision !== review.project.revision
    || compiled.proposal.base_project_revision !== undefined
      && compiled.proposal.base_project_revision !== review.project.revision
  ) {
    throw new Error("项目内容已在生成期间变化，请重新打开当前步骤");
  }
  if (JSON.stringify(compiled.proposal.clips) !== JSON.stringify(review.proposal.clips)) {
    throw new Error("待确认剪辑方案的片段内容不一致");
  }
  return { contentDraftId: displayedContentDraftId, review };
}

function post(payload: object): RequestInit {
  return { method: "POST", body: JSON.stringify(payload) };
}
