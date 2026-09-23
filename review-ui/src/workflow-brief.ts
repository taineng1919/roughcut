import { TICKS_PER_SECOND } from "./player";
import type { SourceBinding } from "./workflow-types";

export type NarrationMode = "同期声为主（无解说）" | "需要解说" | "待定";

export interface BriefFormValues {
  theme: string;
  targetDuration: string;
  contentRequirements: string;
  allowReorder: boolean;
  narrationMode: NarrationMode | "";
}

export interface NormalizedBrief {
  request: {
    theme: string;
    target_duration_ticks: number;
    focus: string[];
    allow_reorder: boolean;
  };
  sourceBindings: SourceBinding[];
  summary: string;
}

export function parseDurationTicks(value: string): number {
  const normalized = value.trim().replaceAll(" ", "");
  let seconds: bigint | null = null;
  let match = /^(\d+)分钟$/.exec(normalized);
  if (match !== null) seconds = BigInt(match[1]!) * 60n;
  match = /^(\d+)分(\d+)秒$/.exec(normalized);
  if (match !== null) {
    const remainder = BigInt(match[2]!);
    if (remainder >= 60n) throw new Error("秒数必须小于 60");
    seconds = BigInt(match[1]!) * 60n + remainder;
  }
  match = /^(\d{1,}):(\d{2})$/.exec(normalized);
  if (match !== null) {
    const remainder = BigInt(match[2]!);
    if (remainder >= 60n) throw new Error("时间格式中的秒数必须小于 60");
    seconds = BigInt(match[1]!) * 60n + remainder;
  }
  match = /^(\d+)秒$/.exec(normalized);
  if (match !== null) seconds = BigInt(match[1]!);
  if (seconds === null) {
    throw new Error("目标时长请填写为“3 分钟”“3 分 30 秒”“03:30”或“210 秒”");
  }
  if (seconds <= 0n) throw new Error("目标时长必须大于 0");
  const ticks = seconds * BigInt(TICKS_PER_SECOND);
  if (ticks > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("目标时长超出安全范围");
  return Number(ticks);
}

export function formatDurationTicks(ticks: number): string {
  if (!Number.isSafeInteger(ticks) || ticks <= 0 || ticks % TICKS_PER_SECOND !== 0) {
    return `${ticks / TICKS_PER_SECOND} 秒`;
  }
  const seconds = ticks / TICKS_PER_SECOND;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes === 0) return `${seconds} 秒`;
  return remainder === 0 ? `${minutes} 分钟` : `${minutes} 分 ${remainder} 秒`;
}

export function normalizeBriefForm(
  values: BriefFormValues,
  bindings: readonly SourceBinding[],
  sourceNames: readonly string[] = [],
): NormalizedBrief {
  const theme = required(values.theme, "主题/目的不能为空");
  const contentRequirements = values.contentRequirements.trim();
  if (!isNarrationMode(values.narrationMode)) throw new Error("请选择解说方式");
  const target = parseDurationTicks(values.targetDuration);
  const focus = [
    `解说方式：${values.narrationMode}`,
    ...(contentRequirements ? [`内容要求：${contentRequirements}`] : []),
  ];
  const summary = [
    `本轮素材：${sourceNames.length > 0 ? sourceNames.join("、") : `${bindings.length} 条已选素材`}`,
    `主题/目的：${theme}`,
    `目标时长：${formatDurationTicks(target)}`,
    `解说方式：${values.narrationMode}`,
    `内容顺序：${values.allowReorder ? "可按表达效果重组（推荐）" : "尽量保持拍摄或事件顺序"}`,
    `内容要求：${contentRequirements || "未填写（可由 Agent 提建议）"}`,
  ].join("\n");
  return {
    request: {
      theme,
      target_duration_ticks: target,
      focus,
      allow_reorder: values.allowReorder,
    },
    sourceBindings: bindings.map((binding) => ({ ...binding })),
    summary,
  };
}

export function formFromBrief(
  brief: {
    theme: string;
    target_duration_ticks: number;
    focus: readonly string[];
    allow_reorder: boolean;
  } | null,
): BriefFormValues {
  const requirements: string[] = [];
  let narrationMode: NarrationMode | "" = "";
  for (const entry of brief?.focus ?? []) {
    if (!entry.trim()) continue;
    const value = valueAfterPrefix(entry, "解说方式：");
    if (value !== null && isNarrationMode(value)) {
      narrationMode = value;
      continue;
    }
    const legacyNarration = valueAfterPrefix(entry, "旁白：");
    if (legacyNarration !== null && legacyNarrationMode(legacyNarration) !== null) {
      narrationMode = legacyNarrationMode(legacyNarration)!;
      continue;
    }
    const content = valueAfterPrefix(entry, "内容要求：");
    requirements.push(content ?? entry);
  }
  return {
    theme: brief?.theme ?? "",
    targetDuration: brief === null ? "" : formatDurationTicks(brief.target_duration_ticks),
    contentRequirements: requirements.join("\n"),
    allowReorder: brief?.allow_reorder ?? true,
    narrationMode,
  };
}

function required(value: string, error: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(error);
  return normalized;
}

function valueAfterPrefix(value: string, prefix: string): string | null {
  return value.startsWith(prefix) ? value.slice(prefix.length).trim() : null;
}

function legacyNarrationMode(value: string): NarrationMode | null {
  if (value === "不需要") return "同期声为主（无解说）";
  if (value === "需要") return "需要解说";
  if (value === "待讨论") return "待定";
  return null;
}

function isNarrationMode(value: string): value is NarrationMode {
  return value === "同期声为主（无解说）" || value === "需要解说" || value === "待定";
}
