---
name: revise-roughcut
description: Guide precise initial-draft changes and roughcut-preview adjustments without bypassing confirmation gates.
---

# Revise roughcut

Use the versioned local roughcut tools and read `references/tool-contract.md`
before changing an Edit. Explain the current user task in ordinary Chinese;
internal IDs, revisions, stages, hashes, receipts, bases, refs, action names
and artifact names belong only in diagnostics.

## 面向用户的引导

先区分两类调整：

- Agent 的初稿局部改稿：用户在同一对话中用自然语言要求局部重写；不要求先在页面
  选中。
- 初稿页面机械调整：删除、移动到光标、从原稿加入、撤销和重做。

初稿中的机械删除、移动和加入仍在“初稿调整”页面完成；Agent 的初稿局部改稿使用
不可变子稿，不得用 Edit 工具绕过初稿确认。粗剪预览与采用页只检查 Virtual Timeline
中的画面、声音、顺序和衔接，并只提供连续文稿定位、返回初稿和采用；不在粗剪页重复
删除、恢复、重排、撤销/重做或 trim。

“继续”“可以”不表示确认初稿、采用粗剪或正式导出。返回初稿并修改后，旧粗剪失效，
需要重新生成。页面或项目 stale 时保留内容供查看但停止写入，不自动重放、合并或覆盖。
Agent 的局部改稿完成后，页面刷新直接读取新的 current child；不得设计“查看新稿/
继续当前稿”选择，也不得自动确认初稿。首版不实现内嵌聊天、不自动注入选区，也不自动
替用户粘贴内容。
用户从初稿页选择“复制给 Agent 调整”后，交接文本会带精确项目引用、Review session、
初稿 candidate ID 和可选引文。必须先用 `content_draft_read` 读取该精确
candidate；不得扫描目录或按时间猜测最新稿。

若当前已批准 Outline 中有用户明确的 chronological、causal 或 progressive 要求，局部改稿和页面编辑
都必须在 readback 前复用同一套 Skill-only explicit order acceptance；只读取 exact approved `outline_ref`、
当前 candidate ID 和 current ordered blocks，不做 Core NLP、文本相似度匹配或自动重排。required item 可以
是 section node，也可以是用户在 Outline 阶段明确声明的 section 内 content node；后者只能复制现有
`required_content_coverage` entry 的完整 `evidence_refs`，并保留临时 `outline_coverage_index`，不能用
`requirement`、标题或自然语言作为匹配键。临时 checklist 不写 Project，结构固定为
`outline_ref`、`candidate_id`、`order_requirement`、`required_items`、`mapping`、`precedence_checks`、
`missing_required_nodes`、`result`；`draft_position` 固定为 `[current_block_index, ref_index]`，按二元组字典序比较。

section node 用批准的 section identity/title 与当前 Draft 独立 heading 做唯一精确匹配；无 heading 报
`missing_required_node`，重复/不唯一报 `mapping_ambiguous`。content node 只在其 section heading range
内的当前有序 `source_excerpt.refs` 或已录音 narration `recorded_refs` 中，按完整
`(source_id, transcript_version_id, segment_id, start_ticks, end_ticks)` 五元组找连续 exact-ref subsequence。
它可以跨没有 heading boundary 的相邻 blocks，mapping 必须记录所有 block IDs 与首个 ref 的
`[block_index, ref_index]`；中间出现另一条 media ref、跳过 ref、跨 heading、未录音 narration 或零候选报
`missing_required_node`，重复 exact sequence/ref 报 `mapping_ambiguous`，均 FAIL。不建 generic causal graph/DAG，不调用 LLM judge，也不增加 Core/workflow gate。按显式 required content
sequence 线性比较二元位置；A→B→C PASS、同章 A→C→B FAIL、causal A→B PASS、progressive 保持顺序 PASS，
同一 content node 跨两个相邻 blocks 且 refs 连续时仍须唯一 mapping；无关 block 插入不改变结果。没有明确顺序要求时结果为 `NOT_APPLICABLE`，不产生假约束；相同
Outline/candidate/blocks 重复 readback 必须确定性一致。candidate stale 或变更后丢弃旧 mapping、重新读取，
不重放旧写入。

## 仅供 Agent 执行的顺序

### Agent 的初稿局部改稿

1. 用户可在同一 Agent 对话中不限次数要求重做开头或结尾、替换金句、增删或重排章节、
   压缩某一章节、补找遗漏内容、平衡人物或素材，或调整解说策略或时长分配。先调用
   `project_open`，再以 `content_draft_read` 读取复制交接、当前 Review session 指向或
   前一轮工具返回的精确 candidate ID 及其完整 blocks；不得扫描文件或按时间猜测“最新稿”。
   用户通常只需自然语言，不要求先在页面选中；范围有歧义时，先复述它理解的修改范围，
   只询问一个真正阻塞的问题。
   对“第一段”“开头”“刚才那段”等相对指代，首次解释相对指代时立即冻结 candidate
   ID、目标 block IDs/exact refs 和短引文。写入前重新读取精确 current candidate，并逐项
   比较冻结目标的身份、位置、refs 与短引文。若目标已经被页面操作删除、移动或替换，
   停止并只询问一个确认问题，说明原目标已变化；必须取得用户对新目标的明确确认，
   不得把原命令静默重绑到当前下一段。
2. 把用户点名的章节或范围映射为非空 mutable block IDs，并提交一份完整 blocks
   snapshot 给 `workflow_action(submit_draft)`；source excerpt 只提交 exact refs，
   正常 schema 2 Agent revise MUST OMIT `canonical_text`，由 Core 从 exact refs 派生；只有明确标记的
   legacy caller 才能提供该字段，且必须严格相等，Agent 不得 join、重写或维护副本。每次先读取 `workflow_status`，使用 exact
   anchor/parent、Brief ref、bindings 与 context，并保留本次 action ID。每轮都以前一轮
   返回的精确 child 为 parent，
   创建不可变 child；stale 或保存失败时停止并重新读取，不重放、合并或覆盖。
3. 默认只修改点名范围。该 application service 会逐字段强制未指定 blocks 的 block ID、
   exact refs、顺序、core 派生文字和 section metadata 保持不变，并拒绝未知 refs、
   未声明变化、stale 和空改动。只有用户明确要求“重新设计整篇”或“重新出一版”时，
   才允许以当前稿为 parent、完整 mutable block scope 调用
   `workflow_action(submit_draft)` 创建全稿变化。
4. 写入后调用 `content_draft_read` 读回新的 current child，向用户回读修改了哪些章节、
   哪些章节未改变，以及 `duration_acceptance` 的 `target_duration_ticks`、
   `actual_duration_ticks`、`delta_ticks`、`status`、`tolerance_ticks` 和
   `accepted_upper_bound_ticks` 摘要及本轮时长变化。
   `delta_ticks` 固定为 `actual - target`；默认接受区间为含边界的
   `target .. target + floor(target * 10 / 100)`，status 只能是 `within_target`、
   `under_target` 或 `over_target`。
   该摘要必须原样使用 Core 的 exact-ref duration truth；Skill 不按字数、自然语言、Agent 估算、
   UI 重算或自己的 timeline 重算。`under_target`/`over_target` 只产生清楚的非阻塞 warning，但不得静默
   approve/adopt：优先 revise；若用户明确接受保留，必须回报 gap/偏差并记录显式接受。要求
   `within_target` 而未满足时停止验收，不能声称满足目标。素材不足时必须明确报告 gap，不能把缺口静默写成满足目标。候选 stale 或 refs 变化后必须重新读取当前 candidate，丢弃旧摘要；页面刷新直接
   读取新的 current child，不得设计
   “查看新稿/继续当前稿”选择，不得自动确认初稿。
   随后用当前 approved Outline 的 `outline_ref` 重建 order checklist，再回读
   `required_items`、`mapping`、`precedence_checks`、`missing_required_nodes` 和 `result`。
   `FAIL` 时只报告逆序、缺失或歧义证据并返回现有改稿路径；不自动重排、不增加 workflow gate，也不把普通
   无顺序稿件变成阻塞门。
5. 用户可继续下一轮，也可撤销刚才的修改。撤销前比较当前 child 与目标 parent：
   mutable block IDs 必须包含当前 child 中所有将改变或消失的 blocks，包括上一轮新增的
   替换 blocks；再用 `workflow_action(submit_draft)` 创建另一个不可变 child，恢复目标
   parent 的完整 blocks。不得删除或覆盖任何历史 artifact。
6. 这一流程不实现内嵌聊天，不自动注入选区，不自动替用户粘贴内容。

### 从粗剪返回初稿

1. 用户在粗剪预览中发现内容、顺序或气口问题时，选择“返回初稿调整”。重新读取
   `workflow_status` 的 current subject 与 confirmed Draft ref，并调用
   `workflow_action(return_to_draft)`。该动作只把当前 Review session 切回精确 Content
   Draft，不创建 child、不确认初稿、不增加 revision。
2. 用户第一次在初稿页面实际删除、移动或加入，或要求 Agent 做局部改稿时，才从该精确
   candidate 创建不可变 child。旧 Proposal 和 Decision 保持字节不变。
3. 新 child 必须通过 `workflow_action(approve_draft)` 重新确认并生成新的粗剪预览；旧
   采用结果不得迁移到新预览。用户完整播放核对后，仍须重新选择“采用这个粗剪版本”，
   并通过 `workflow_action(adopt_roughcut)` 提交。
4. 既有 `edit_change`、`edit_undo`、`edit_redo` 与 history/schema 继续兼容旧调用，但不
   作为首版普通粗剪页面的编辑入口，也不得用它们绕过返回初稿和重新确认。

不要引入拖拽、段落手柄、前后移动按钮、右键菜单、自动 filler-word 删除、B-roll、
自由改写同期声或直接 FFmpeg 调用。
