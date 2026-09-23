import { describe, expect, it } from "vitest";

import {
  PuncSession,
  validatePunctuationReplacement,
} from "./draft-punctuation-editor";

describe("controlled punctuation input validation", () => {
  it("accepts punctuation-only input without changing non-punctuation text", () => {
    expect(validatePunctuationReplacement("你好", 2, 2, "！", "typing")).toEqual({
      accepted: true,
      value: "你好！",
    });
  });

  it("rejects the ninth punctuation in a changed run while retaining the accepted value", () => {
    const result = validatePunctuationReplacement("你好。。。。。。。。", 10, 10, "！", "typing");
    expect(result).toEqual({
      accepted: false,
      value: "你好。。。。。。。。",
      message: "一个位置最多输入 8 个标点；多余内容未加入。",
    });
  });

  it("rejects an overflowing paste as a whole instead of truncating it", () => {
    expect(validatePunctuationReplacement("你好。。。。。。", 8, 8, "！？。", "paste")).toEqual({
      accepted: false,
      value: "你好。。。。。。",
      message: "一个位置最多输入 8 个标点；本次粘贴未加入。",
    });
  });

  it("rejects non-punctuation input and preserves the prior value", () => {
    expect(validatePunctuationReplacement("你好", 2, 2, "字", "paste")).toEqual({
      accepted: false,
      value: "你好",
      message: "正文原话不能直接改写；识别错误请校正原稿。",
    });
  });

  it("keeps one session local and emits one exact five-field payload", () => {
    const session = new PuncSession({
      candidateId: "draft_a",
      checkpoint: { generation: 2, checkpoint_hash: "a".repeat(64) },
      currentCandidate: {
        artifact_id: "draft_a",
        schema_version: 2,
        content_hash: "b".repeat(64),
      },
      paragraphId: "paragraph_a",
      blockId: "block_a",
      startUtf16Offset: 2,
      endUtf16Offset: 2,
    }, "你好");
    expect(session.apply(2, 2, "！！", "typing").accepted).toBe(true);
    expect(session.apply(4, 4, "？", "typing").accepted).toBe(true);
    expect(session.value).toBe("你好！！？");
    expect(session.payload()).toEqual({
      paragraph_id: "paragraph_a",
      block_id: "block_a",
      start_utf16_offset: 2,
      end_utf16_offset: 2,
      replacement: "！！？",
    });
  });

  it("rejects a UTF-16 surrogate midpoint without changing the session", () => {
    const session = new PuncSession({
      candidateId: "draft_a",
      checkpoint: { generation: 2, checkpoint_hash: "a".repeat(64) },
      currentCandidate: {
        artifact_id: "draft_a",
        schema_version: 2,
        content_hash: "b".repeat(64),
      },
      paragraphId: "paragraph_a",
      blockId: "block_a",
      startUtf16Offset: 3,
      endUtf16Offset: 3,
    }, "你😀好");
    const result = session.apply(2, 2, "！", "typing");
    expect(result.accepted).toBe(false);
    expect(session.value).toBe("你😀好");
    expect(session.payload()).toBeNull();
  });

  it("validates the final IME composition text as punctuation", () => {
    expect(validatePunctuationReplacement("你好", 2, 2, "。！？", "composition")).toEqual({
      accepted: true,
      value: "你好。！？",
    });
    expect(validatePunctuationReplacement("你好", 2, 2, "啊。", "composition")).toEqual({
      accepted: false,
      value: "你好",
      message: "正文原话不能直接改写；识别错误请校正原稿。",
    });
  });

  it("deletes only a selected persistent punctuation code point", () => {
    const session = new PuncSession({
      candidateId: "draft_a",
      checkpoint: { generation: 2, checkpoint_hash: "a".repeat(64) },
      currentCandidate: {
        artifact_id: "draft_a",
        schema_version: 2,
        content_hash: "b".repeat(64),
      },
      paragraphId: "paragraph_a",
      blockId: "block_a",
      startUtf16Offset: 2,
      endUtf16Offset: 3,
    }, "你好！");
    expect(session.apply(2, 3, "", "typing")).toEqual({
      accepted: true,
      value: "你好",
    });
    expect(session.payload()).toEqual({
      paragraph_id: "paragraph_a",
      block_id: "block_a",
      start_utf16_offset: 2,
      end_utf16_offset: 3,
      replacement: "",
    });
  });

  it("expands the payload when backspace removes adjacent persistent punctuation", () => {
    const session = new PuncSession({
      candidateId: "draft_a",
      checkpoint: { generation: 2, checkpoint_hash: "a".repeat(64) },
      currentCandidate: {
        artifact_id: "draft_a",
        schema_version: 2,
        content_hash: "b".repeat(64),
      },
      paragraphId: "paragraph_a",
      blockId: "block_a",
      startUtf16Offset: 3,
      endUtf16Offset: 3,
    }, "你好！");
    expect(session.apply(2, 3, "", "typing").accepted).toBe(true);
    expect(session.payload()).toEqual({
      paragraph_id: "paragraph_a",
      block_id: "block_a",
      start_utf16_offset: 2,
      end_utf16_offset: 3,
      replacement: "",
    });
  });

  it("freezes the basis and coordinates supplied when the session starts", () => {
    const binding = {
      candidateId: "draft_a",
      checkpoint: { generation: 2, checkpoint_hash: "a".repeat(64) },
      currentCandidate: {
        artifact_id: "draft_a",
        schema_version: 2,
        content_hash: "b".repeat(64),
      },
      paragraphId: "paragraph_a",
      blockId: "block_a",
      startUtf16Offset: 2,
      endUtf16Offset: 2,
    };
    const session = new PuncSession(binding, "你好");
    binding.candidateId = "draft_new";
    binding.checkpoint.generation = 9;
    binding.currentCandidate.artifact_id = "draft_new";
    binding.paragraphId = "paragraph_new";
    binding.blockId = "block_new";

    expect(session.apply(2, 2, "！", "typing").accepted).toBe(true);
    expect(session.b).toEqual({
      candidateId: "draft_a",
      checkpoint: { generation: 2, checkpoint_hash: "a".repeat(64) },
      currentCandidate: {
        artifact_id: "draft_a",
        schema_version: 2,
        content_hash: "b".repeat(64),
      },
      paragraphId: "paragraph_a",
      blockId: "block_a",
      startUtf16Offset: 2,
      endUtf16Offset: 2,
    });
    expect(session.payload()?.paragraph_id).toBe("paragraph_a");
    expect(session.payload()?.block_id).toBe("block_a");
  });
});
