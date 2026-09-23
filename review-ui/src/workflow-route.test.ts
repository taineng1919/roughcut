import { describe, expect, it } from "vitest";

import {
  WORKFLOW_STAGE_ORDER,
  WorkflowRouteState,
  routeFromHash,
} from "./workflow-route";

describe("workflow stage routes", () => {
  it("starts at overview and recognizes only the fixed focused stages", () => {
    expect(routeFromHash("")).toBe("overview");
    expect(routeFromHash("#workflow/brief")).toBe("brief");
    expect(routeFromHash("#workflow/transcript")).toBe("transcript");
    expect(routeFromHash("#workflow/unknown")).toBe("overview");
    expect(WORKFLOW_STAGE_ORDER.map((stage) => stage.title)).toEqual([
      "选择素材",
      "准备素材",
      "阅读原始转录稿",
      "确定剪辑要求",
      "调整初稿",
      "确认剪辑方案",
      "调整已确认剪辑",
      "正式导出",
    ]);
  });

  it("keeps exactly one focused workspace visible across route restoration", () => {
    const routes = new WorkflowRouteState("#workflow/brief");
    expect(routes.active).toBe("brief");
    expect(routes.visibleWorkspaceIds()).toEqual(["workflow-stage-brief"]);
    routes.restore("#workflow/transcript");
    expect(routes.visibleWorkspaceIds()).toEqual(["workflow-stage-transcript"]);
    routes.restore("#workflow");
    expect(routes.visibleWorkspaceIds()).toEqual(["workflow-stage-overview"]);
  });
});
