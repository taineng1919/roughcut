export type WorkflowRoute =
  | "overview"
  | "sources"
  | "prepare"
  | "transcript"
  | "brief"
  | "draft"
  | "proposal"
  | "decision"
  | "render";

export const WORKFLOW_STAGE_ORDER = [
  { key: "sources", title: "选择素材" },
  { key: "prepare", title: "准备素材" },
  { key: "transcript", title: "阅读原始转录稿" },
  { key: "brief", title: "确定剪辑要求" },
  { key: "draft", title: "调整初稿" },
  { key: "proposal", title: "确认剪辑方案" },
  { key: "decision", title: "调整已确认剪辑" },
  { key: "render", title: "正式导出" },
] as const;

const ROUTES = new Set<WorkflowRoute>([
  "overview", ...WORKFLOW_STAGE_ORDER.map((stage) => stage.key),
]);

export function routeFromHash(hash: string): WorkflowRoute {
  const match = /^#workflow(?:\/([a-z-]+))?$/.exec(hash);
  const candidate = match?.[1] ?? "overview";
  return ROUTES.has(candidate as WorkflowRoute) ? candidate as WorkflowRoute : "overview";
}

export function hashForRoute(route: WorkflowRoute): string {
  return route === "overview" ? "#workflow" : `#workflow/${route}`;
}

export class WorkflowRouteState {
  private route: WorkflowRoute;

  constructor(hash: string) {
    this.route = routeFromHash(hash);
  }

  get active(): WorkflowRoute {
    return this.route;
  }

  restore(hash: string): void {
    this.route = routeFromHash(hash);
  }

  visibleWorkspaceIds(): string[] {
    return [`workflow-stage-${this.route}`];
  }
}
