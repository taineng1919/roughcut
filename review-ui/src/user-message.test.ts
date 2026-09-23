import { describe, expect, it } from "vitest";

import { userFacingErrorMessage } from "./user-message";

describe("user-facing review errors", () => {
  it.each([
    "workflow Content Draft blocks must be an object array",
    "proposal unexpectedly changed project revision",
    "文稿分页包含重复 paragraph identity",
    "trim bounds must use integer ticks",
    "content draft Transcript binding is not active",
    "Roughcut Draft workspace refused a stale checkpoint generation",
  ])("hides internal implementation wording: %s", (message) => {
    const result = userFacingErrorMessage({ message });

    expect(result).toBe(
      "操作未完成。项目内容或页面状态可能已经变化，请重新打开当前步骤后再试。",
    );
    expect(result).not.toMatch(
      /Content Draft|Proposal|revision|paragraph identity|ticks/i,
    );
  });

  it("keeps a specific ordinary-language validation message", () => {
    expect(userFacingErrorMessage(new Error("初稿不能为空"))).toBe("初稿不能为空");
  });
});
