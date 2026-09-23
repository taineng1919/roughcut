import { describe, expect, it, vi } from "vitest";

import { copyDraftAgentHandoff } from "./agent-handoff";
import type { WorkflowApi } from "./workflow-types";

describe("exact draft Agent clipboard handoff", () => {
  it("requests the exact candidate and copies only the user-triggered response", async () => {
    const api = vi.fn(async (_path: string, options?: RequestInit) => {
      expect(_path).toBe("/api/workflow/draft-agent-handoff");
      expect(JSON.parse(String(options?.body))).toEqual({
        candidate_id: "draft_exact",
        quote: "当前选中的原话",
      });
      return {
        handoff: {
          candidate_id: "draft_exact",
          session_id: "review_session_exact",
          text: "精确交接文本",
        },
      };
    }) as WorkflowApi;
    const writeText = vi.fn(async () => undefined);

    const result = await copyDraftAgentHandoff(
      api,
      { writeText },
      "draft_exact",
      "当前选中的原话",
    );

    expect(result).toEqual({
      ok: true,
      message: "已复制，请回到原 Agent 对话粘贴。",
    });
    expect(writeText).toHaveBeenCalledOnce();
    expect(writeText).toHaveBeenCalledWith("精确交接文本");
  });

  it("reports clipboard rejection without claiming success", async () => {
    const api = (async () => ({
      handoff: {
        candidate_id: "draft_exact",
        session_id: "review_session_exact",
        text: "精确交接文本",
      },
    })) as WorkflowApi;

    const result = await copyDraftAgentHandoff(
      api,
      { writeText: async () => Promise.reject(new Error("denied")) },
      "draft_exact",
      null,
    );

    expect(result.ok).toBe(false);
    expect(result.message).toContain("未能复制");
    expect(result.message).toContain("剪贴板");
  });

  it("fails closed when Clipboard is unavailable or the response candidate changed", async () => {
    const unavailable = await copyDraftAgentHandoff(
      (async () => {
        throw new Error("must not call");
      }) as WorkflowApi,
      null,
      "draft_exact",
      null,
    );
    expect(unavailable).toEqual({
      ok: false,
      message: "当前浏览器无法使用剪贴板，请检查权限后重试。",
    });

    const mismatch = await copyDraftAgentHandoff(
      (async () => ({
        handoff: {
          candidate_id: "draft_new",
          session_id: "review_session_exact",
          text: "错误候选",
        },
      })) as WorkflowApi,
      { writeText: async () => undefined },
      "draft_exact",
      null,
    );
    expect(mismatch.ok).toBe(false);
    expect(mismatch.message).toContain("初稿已变化");
  });

  it("keeps a stale candidate failure next to the copy action", async () => {
    const stale = await copyDraftAgentHandoff(
      (async () => Promise.reject({
        code: "invalid_workflow_change",
        message: "draft editor candidate is stale",
      })) as WorkflowApi,
      { writeText: async () => undefined },
      "draft_exact",
      "可见引文",
    );

    expect(stale).toEqual({
      ok: false,
      message: "初稿已变化，未复制交接内容；请刷新后重试。",
    });
  });
});
