import type { WorkflowApi } from "./workflow-types";

interface ClipboardPort {
  writeText(text: string): Promise<void>;
}

interface AgentHandoffResponse {
  handoff: {
    candidate_id: string;
    session_id: string;
    text: string;
  };
}

export type AgentHandoffCopyResult =
  | { ok: true; message: string }
  | { ok: false; message: string };

export async function copyDraftAgentHandoff(
  api: WorkflowApi,
  clipboard: ClipboardPort | null,
  candidateId: string,
  quote: string | null,
): Promise<AgentHandoffCopyResult> {
  if (clipboard === null) {
    return {
      ok: false,
      message: "当前浏览器无法使用剪贴板，请检查权限后重试。",
    };
  }
  try {
    const response = await api<AgentHandoffResponse>(
      "/api/workflow/draft-agent-handoff",
      {
        method: "POST",
        body: JSON.stringify({
          candidate_id: candidateId,
          quote: quote?.trim() || null,
        }),
      },
    );
    if (response.handoff.candidate_id !== candidateId) {
      return {
        ok: false,
        message: "初稿已变化，未复制交接内容；请刷新后重试。",
      };
    }
    await clipboard.writeText(response.handoff.text);
    return {
      ok: true,
      message: "已复制，请回到原 Agent 对话粘贴。",
    };
  } catch (error) {
    if (isStaleCandidateError(error)) {
      return {
        ok: false,
        message: "初稿已变化，未复制交接内容；请刷新后重试。",
      };
    }
    return {
      ok: false,
      message: "未能复制到剪贴板，请检查浏览器权限后重试。",
    };
  }
}

function isStaleCandidateError(error: unknown): boolean {
  if (typeof error !== "object" || error === null || !("code" in error)) return false;
  const payload = error as { code?: unknown; message?: unknown };
  return payload.code === "stale_review"
    || (
      payload.code === "invalid_workflow_change"
      && payload.message === "draft editor candidate is stale"
    );
}
