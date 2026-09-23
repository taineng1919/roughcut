import { describe, expect, it } from "vitest";

import { ADOPTED_EXPORT_GUIDANCE } from "./roughcut";

describe("adopted roughcut export guidance", () => {
  it("keeps adoption and formal export as separate user actions", () => {
    expect(ADOPTED_EXPORT_GUIDANCE).toBe(
      "当前粗剪版本已采用。需要正式导出时，请回到同一 Agent 对话并提出：正式导出当前已采用的粗剪版本。",
    );
    expect(ADOPTED_EXPORT_GUIDANCE).not.toContain("确认渲染");
  });
});
