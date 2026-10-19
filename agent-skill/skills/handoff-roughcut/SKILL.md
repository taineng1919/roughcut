---
name: handoff-roughcut
description: Export a Roughcut transcript for an external editor or model and resolve the returned final script back into current Draft submission blocks.
---

# Roughcut 外部定稿交接

这是一个辅助 Skill，不是第二套粗剪流程。只处理“导出转录 → 外部修改文稿 → 导回”
这一条媒体无关链路；Brief、Outline、Draft 确认、粗剪采用和正式导出仍走
`create-roughcut`、`revise-roughcut`、`render-roughcut` 的现有门禁。

脚本 `scripts/roughcut_handoff.py` 只使用 Python 标准库和已安装的绝对粗剪 CLI，
不导入源码 checkout、不写 Project JSON、不提交 Draft。不能搜索 PATH 或源码定位
CLI；只使用安装诊断已经提供的绝对 `roughcut` 路径。若宿主只有 MCP 而没有该绝对
路径，停止导出并报告缺失，不要伪造 Resolver 结果。

## 触发与反馈

用户自然表达“把转录文本导出给其他模型参考组稿/改稿/写稿”“整理给外部编辑”
等意图时直接执行本 Skill；不要求固定提示词，不要求用户逐项列资料或编写技术指令。
使用当前已授权项目和内容范围自动整理已有 Brief、Outline 与背景资料；缺少时按
“未提供/待讨论”继续，只有无法确定项目或素材范围时才询问。

导出成功后必须向用户回报包路径和三个附件，并直接展示 export 结果中的
`forward_message` 文本，供用户复制转发。不要只给路径，也不要让用户自己阅读说明后
转述；不得自动向第三方发消息。

## 导出

当前授权 content bindings 的转录已就绪且存在有效 WorkflowRun 时即可导出；导出是
只读文稿交接，不新增 Brief/Outline 审批，也不以 stage 推断许可。脚本自动调用
`workflow-status`，使用 exact ordered transcript bindings，并把
`artifact_refs.outline.snapshot` 与 `approval_statuses` 渲染为“已有大纲/Brief 及
是否已批准”。缺失时写“未提供/待讨论”，供外部模型参与提纲创作。不得退回项目全部
素材或手工覆盖大纲。

背景资料只能来自用户明确给出的文件。不要遍历目录、猜测附件或自行补充。

```sh
python3 <skill-dir>/scripts/roughcut_handoff.py export \
  --project /absolute/project \
  --package /absolute/new-handoff-package \
  --cli /absolute/installed/roughcut \
  [--background /absolute/user-provided-background.md]
```

包内固定生成 transcript.md、transcript.map.json、handoff.md。`package_id`
来自现有 Markdown 导出的内容 hash；脚本会核对 map、实时 readable view、revision、
view hash 和 workflow run/bindings。包目录必须尚不存在。导出不修改项目，也不新增
完整 readable JSON 快照或素材数据库。

把这三个文件交给用户已有的同步目录或连接器分发。不要开发 Drive 同步、凭据或 API。
导出反馈使用结果中的 `forward_message`；不要把任何指引追加进 transcript.md，也不要
新增第四个文件。

## 最终交接稿格式

创作/讨论过程可以使用自然语言，不必全程只输出 JSON。只有用户确认定稿后的最终
交接稿才返回一个 JSON：顶层 `package_id` 和有序 `blocks`。block 三选一：
`section_title`、`narration`、`source_excerpt`。`source_excerpt` 只写本包 `P###`
加可选 `quote`；引用整段时省略 quote。重复 quote 必须给 `occurrence`，0 起算。
跨 P、跳段或换素材必须拆成多个 source_excerpt blocks。不得填写 ticks、refs、
字符 offset、媒体路径或 Core 字段。

```json
{
  "package_id": "<handoff.md 中的 package_id>",
  "blocks": [
    {"section_title": "开场"},
    {"narration": "未录解说文字。"},
    {"source_excerpt": {"paragraph": "P001"}},
    {"source_excerpt": {"paragraph": "P003", "quote": "重复出现的完整原话", "occurrence": 1}}
  ]
}
```

quote 必须与实时转录逐字一致，不改写、不纠错、不补省略号，也不做模糊匹配。
没有 quote 的条目按整段引用。

## 导回

```sh
python3 <skill-dir>/scripts/roughcut_handoff.py resolve \
  --project /absolute/project \
  --package /absolute/handoff-package \
  --draft /absolute/external-final.json \
  --output /absolute/outside/project/candidate.json \
  --cli /absolute/installed/roughcut
```

脚本按 blocks 原顺序逐条调用现有 `transcript-selection-resolve`，不会先把整稿交给
Resolver 再采用其返回排序。输出只包含候选 schema 2 blocks 和依据；不提交、不确认、
不修改项目。`--output` 必须位于项目与包目录之外，且不能是定稿输入本身。

如果输出 `ok: false`：

- `quote_not_found`、`quote_occurrence_required`、`occurrence_invalid`：要求用户
  对照转录修正；绝不默认第一处或清洗文本。
- `expanded_to_segments`：展示 requested 与 resolved 原文、refs 和 warnings。用户
  接受实际范围时，要求把该条 quote 改成工具返回的完整原文后重新导回；否则另选引用。
  不自行扩大范围，也不强制改成整 P。
- `package_stale`、`package_tampered`、`package_id_mismatch`、`project_mismatch`：
  停止并从当前状态重新导出/核对，不复用旧包或旧回执。

## 导回后的 Draft 门禁

`ok: true` 只代表候选 blocks 已准备好，不代表提交成功。先重新读取当前
`workflow_status`，确认仍是同一 run、`draft_review`、当前 Brief/Outline/context
bindings 未 stale；按现有初稿路径取得 `brief_ref`、`context_hash`、当前 parent
draft 和 display title，再调用 `workflow_action(submit_draft)`。不要用脚本输出替代
Brief/Outline 门禁，也不要直接调用低层 Proposal 写入口。

提交后调用 `content_draft_read` 核对 canonical 原文、顺序、选段和
`duration_acceptance`。外部定稿不是 exact candidate 的已批准；仍需现有初稿确认、
采用和导出授权。未录解说保持 narration，由 Core 在后续粗剪候选中自动生成
placeholder；不要伪造语音、转录或 ticks。
