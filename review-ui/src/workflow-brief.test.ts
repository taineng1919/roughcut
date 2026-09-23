import { describe, expect, it } from "vitest";

import {
  formFromBrief,
  normalizeBriefForm,
  parseDurationTicks,
} from "./workflow-brief";

const bindings = [
  { source_id: "src_b", transcript_version_id: "tr_b" },
  { source_id: "src_a", transcript_version_id: "tr_a" },
];

describe("guided workflow Brief", () => {
  it("normalizes required and optional fields without changing ordered bindings", () => {
    const normalized = normalizeBriefForm({
      theme: "  校园探访精华  ",
      targetDuration: "3 分 30 秒",
      contentRequirements: "  必须保留：学校特色与真实互动  ",
      allowReorder: true,
      narrationMode: "待定",
    }, bindings, ["探校开场", "校园参观"]);

    expect(normalized.request).toEqual({
      theme: "校园探访精华",
      target_duration_ticks: 25_200_000,
      focus: [
        "解说方式：待定",
        "内容要求：必须保留：学校特色与真实互动",
      ],
      allow_reorder: true,
    });
    expect(normalized.sourceBindings).toEqual(bindings);
    expect(normalized.sourceBindings).not.toBe(bindings);
    expect(normalized.summary).toContain("3 分 30 秒");
    expect(normalized.summary).toContain("可按表达效果重组（推荐）");
    expect(normalized.summary).toContain("探校开场、校园参观");
  });

  it("accepts natural duration forms and rejects ambiguous or non-positive input", () => {
    expect(parseDurationTicks("3 分钟")).toBe(21_600_000);
    expect(parseDurationTicks("3 分 30 秒")).toBe(25_200_000);
    expect(parseDurationTicks("03:30")).toBe(25_200_000);
    expect(parseDurationTicks("210 秒")).toBe(25_200_000);
    for (const value of ["", "0 秒", "-3 分钟", "3", "3.5 分钟", "3:70", "三分钟"]) {
      expect(() => parseDurationTicks(value)).toThrow();
    }
    expect(() => normalizeBriefForm({
      theme: " ",
      targetDuration: "30 秒",
      contentRequirements: "重点",
      allowReorder: false,
      narrationMode: "同期声为主（无解说）",
    }, bindings)).toThrow("主题/目的不能为空");
    expect(() => normalizeBriefForm({
      theme: "主题",
      targetDuration: "30 秒",
      contentRequirements: "重点",
      allowReorder: false,
      narrationMode: "",
    }, bindings)).toThrow("请选择解说方式");
    const optional = normalizeBriefForm({
      theme: "主题",
      targetDuration: "30 秒",
      contentRequirements: "",
      allowReorder: true,
      narrationMode: "待定",
    }, bindings);
    expect(optional.request.focus).toEqual(["解说方式：待定"]);
    expect(optional.summary).toContain("未填写（可由 Agent 提建议）");
  });

  it("keeps every legacy focus entry visible when hydrating an existing Brief", () => {
    const values = formFromBrief({
      theme: "主题",
      target_duration_ticks: 3_600_000,
      focus: ["重点：第一项", "旁白：待讨论", "旧版自由重点"],
      allow_reorder: false,
    });
    expect(values.targetDuration).toBe("30 秒");
    expect(values.narrationMode).toBe("待定");
    expect(values.contentRequirements).toContain("重点：第一项");
    expect(values.contentRequirements).toContain("旧版自由重点");
    const normalized = normalizeBriefForm(values, bindings);
    expect(normalized.request.focus.join("\n")).toContain("重点：第一项");
    expect(normalized.request.focus.join("\n")).toContain("旧版自由重点");
  });
});
