import "./style.css";

import { RoughcutReviewController } from "./roughcut";
import { WorkflowReviewController } from "./workflow";
import type { WorkflowProposalHandoff } from "./workflow-handoff";
import {
  initialDraftEditorSnapshot,
  reviewMode,
} from "./workflow-state";
import { userFacingErrorMessage } from "./user-message";
import type { ReviewPayload } from "./types";
import type {
  DraftEditorSnapshot,
  WorkflowReviewPayload,
} from "./workflow-types";

const ROUGHCUT_REVIEW_MODE = "roughcut-review-mode";
const WORKFLOW_REVIEW_ID = "workflow-review";
const ARTIFACT_REVIEW_ID = "artifact-review";

async function startReview(): Promise<void> {
  const draftEditor = await initialDraftEditorSnapshot(api);
  if (draftEditor !== null) {
    await mountDraftEditor(draftEditor);
    return;
  }
  const initial = await api<ReviewPayload | WorkflowReviewPayload>("/api/review");
  if (reviewMode(initial) === "workflow") {
    await mountDraftEditor(initial as WorkflowReviewPayload);
    return;
  }
  await mountRoughcut(initial as ReviewPayload);
}

async function mountDraftEditor(
  initial: DraftEditorSnapshot | WorkflowReviewPayload,
): Promise<void> {
  document.body.classList.remove(ROUGHCUT_REVIEW_MODE);
  element<HTMLElement>(ARTIFACT_REVIEW_ID).hidden = true;
  element<HTMLElement>(WORKFLOW_REVIEW_ID).hidden = false;
  const controller = new WorkflowReviewController(
    initial,
    api,
    setStatus,
    handoffToRoughcut,
  );
  await controller.mount();
}

async function handoffToRoughcut(handoff: WorkflowProposalHandoff): Promise<void> {
  try {
    await mountRoughcut(handoff.review);
    setStatus(
      "已进入粗剪预览。请播放并调整；尚未采用当前粗剪，也没有开始正式导出。",
    );
  } catch (error) {
    document.body.classList.remove(ROUGHCUT_REVIEW_MODE);
    document.body.classList.add("draft-editor-mode");
    element<HTMLElement>(WORKFLOW_REVIEW_ID).hidden = false;
    const artifact = element<HTMLElement>(ARTIFACT_REVIEW_ID);
    artifact.hidden = true;
    artifact.replaceChildren();
    throw error;
  }
}

async function mountRoughcut(review: ReviewPayload): Promise<void> {
  document.body.classList.remove("draft-editor-mode");
  document.body.classList.add(ROUGHCUT_REVIEW_MODE);
  element<HTMLElement>(WORKFLOW_REVIEW_ID).hidden = true;
  const artifact = element<HTMLElement>(ARTIFACT_REVIEW_ID);
  if (artifact.childElementCount === 0) {
    const template = element<HTMLTemplateElement>("roughcut-review-template");
    artifact.append(template.content.cloneNode(true));
  }
  artifact.hidden = false;
  const controller = new RoughcutReviewController(
    review,
    api,
    setStatus,
    () => location.reload(),
  );
  await controller.mount();
}

async function api<T = unknown>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
  });
  const payload = (await response.json()) as T & {
    error?: { code: string; message: string; current_revision?: number };
  };
  if (!response.ok) throw payload.error ?? new Error(`HTTP ${response.status}`);
  return payload;
}

function setStatus(text: string, warning = false): void {
  const status = element<HTMLElement>("status");
  status.textContent = text;
  status.classList.toggle("warning", warning);
}

function showStartupError(error: unknown): void {
  setStatus(userFacingErrorMessage(error), true);
}

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (found === null) throw new Error(`missing #${id}`);
  return found as T;
}

if (location.search.includes("token=")) history.replaceState({}, "", "/");
void startReview().catch(showStartupError);
