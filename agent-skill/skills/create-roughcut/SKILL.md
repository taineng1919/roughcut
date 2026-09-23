---
name: create-roughcut
description: Confirm roughcut requirements, prepare a truthful initial draft, and open the initial-draft page at the correct gate.
---

# Create roughcut

Use the versioned local roughcut tools. Do not edit project artifacts directly,
derive context from media paths, or expose internal stages, hashes, receipts,
bases, refs, action names or object names as user workflow steps. This Skill
starts after `roughcut-basics` has established the user-confirmed source scope.

## 面向用户的引导

如果用户提出多机位需求，先确认主机位/副机位分组与素材授权。分组确认本身不会立即对轨
或生成视频。进入当前 Project 的素材集合和进入 ASR/content scope 的 Source 集合是两个独立集合：
用户确认进入 Project 的全部媒体必须先导入 Project，之后才用 `source_authorizations`/`transcribe`
决定哪些已导入 Source 进入内容/ASR，不能反过来过滤 Project import。只有主粗剪已采用且相关授权仍然有效，
才启动一次对轨。不得替用户决定切机、替换或混音。副机位不做 ASR、不进入内容整理，也不参与大纲、初稿或成片内容决定；
声音对轨直接使用原 Source 音轨，不要求副机位 Transcript。说话人区分只是主收声 Source 的参数，
不会把副机位加入 ASR 范围。

用户只看到“剪辑要求”和“初稿”。先说明已有准备结果与需要确认的覆盖、低可信、未
对应人物、排除范围和无语音范围；不要求用户逐页通读原始转录稿。完整原始转录稿仍可
搜索、按需查看或导出。

多机位的 Auxiliary Source 只通过已确认的 `multicam_setup` 和显式 exact `source_pairs`
进入机位 setup；它不得进入 `source_authorizations`，即使 `transcribe=false` 也必须 fail closed，
不产生 ASR、Transcript 或 readiness 要求。不得按文件名、index 或隐含顺序补绑定。

只追问真正影响结果的信息。先说明“下面是可复制修改的模板；可以只改确定项，
不确定处填写‘请 Agent 建议’”，也接受自然语言回复：

```text
主题：请 Agent 根据完整文稿推荐 / 用户填写
成片类型：人物专访 / 新闻专题 / 纪录短片 / 活动回顾 / 新媒体短视频 / 请 Agent 推荐（单选或最多两项）
表达风格：克制纪实 / 温暖叙事 / 专业清晰 / 节奏明快 / 信息密度高 / 请 Agent 推荐（可多选）
解说方式：无解说，以同期声为主 / 需要解说 / 请 Agent 推荐（解说：无解说，以同期声为主 / 需要解说 / 请 Agent 推荐）
内容顺序：允许按主题重组 / 尽量保持原顺序 / 请 Agent 推荐
开场方式：直接进入主题 / 主持人引入 / 金句快剪 / 请 Agent 推荐
必须保留 / 必须避免：没有可写“无”，不确定可写“请 Agent 建议”
建议保留：人物身份与背景 / 核心观点 / 事实与数字 / 代表性原话 / 案例与结果 / 结尾 / 请 Agent 推荐
建议排除：记者或编导提问 / 重复表达与重说 / 明显无关内容 / 过多语气词 / 请 Agent 推荐
目标时长：X 分 X 秒
其他要求：
```

主题表示具体内容主题，成片类型表示作品类型，两者不能互相替代。Agent 读取完整授权文稿后可以先给出
1–3 个主题建议；用户确认后，剪辑要求中的主题必须保存为用户最终确认的具体主题。
先提供推荐方案，用户可修改选项。目标时长是粗剪下限：默认范围为目标时长至目标时长 +10%，
可选宽松范围至 +20%；不得生成短于目标的粗剪。真实可用内容不足时先报告缺口，不能填充、虚构
或机械重复；不修改剪辑要求结构或既有软目标校验器。先回读剪辑要求摘要，只整理用户输入、Agent
建议项和仍待说明的事实缺口；不在这里提前提出开头、主体、结尾或大纲。等待用户确认后才保存剪辑
要求；在这次确认前不生成剪辑方案、不采用粗剪、也不正式导出。
初稿提交和每次改稿后，必须直接读取 `content_draft_read` 返回的 `duration_acceptance`，并向用户
回读 `target_duration_ticks`、`actual_duration_ticks`、`delta_ticks`、`status`、
`tolerance_ticks` 和 `accepted_upper_bound_ticks`。`delta_ticks` 固定为 `actual - target`；
默认接受区间是含边界的 `target .. target + floor(target * 10 / 100)`，status 只能是
`within_target`、`under_target` 或 `over_target`。actual 只信任当前候选 exact refs 的 Core
duration truth，不用字数、自然语言、Agent 估算或另一套 timeline/UI 重算。
under/over 是非阻塞 warning，但不得静默 approve/adopt：优先回到改稿路径；若用户明确接受保留
under/over，必须回报 gap/偏差并记录这次显式接受。凡用户要求 `within_target` 而 status 未达到，
必须停止验收，不能用素材不足、Skill 摘要或自然语言声称满足目标。候选 stale 或 refs
变化后停止旧 readback，重新读取当前 candidate；不得复用旧摘要或自动重放写入。
如果用户明确声明 chronological、causal 或 progressive 要求（例如“按时间顺序”“先因后果”“先基础再步骤”，
而不是 Agent 从内容推断），大纲确认后、初稿验收前必须执行一次 Skill-only 的 explicit order acceptance。
它只使用 exact approved `outline_ref`、当前 `candidate_id` 和 Draft 当前有序 `blocks`，不做 Core NLP、
文本相似度匹配或自动重排。没有用户明确顺序要求时 `order_requirement` 为 `none`，结果为
`NOT_APPLICABLE`，不得凭大纲章节或 `allow_reorder` 猜出约束。

required item 可以是一个 section node，也可以是一个 section 内 content node。content node 只能在 Outline
阶段由用户明确声明为 chronological/causal/progressive 线性序列的一项，并绑定一个现有 Outline schema 1
`required_content_coverage` entry 的完整 `evidence_refs`；临时 checklist 只复制 exact refs 和该 entry 的
index，`requirement`/标题只用于回读，绝不是匹配键。没有用户明确节点或没有可证明的 exact refs 时不创建
节点，报告 `missing_required_node` 或 `mapping_ambiguous`，不能从摘要、原文相似度或 Agent 语义推断补齐。

临时 checklist（不写入 Project、Outline 或 Draft artifact）固定使用以下结构；`draft_position` 是
`[current_block_index, ref_index_in_block]`，按二元组字典序比较，section heading 的 ref index 为 `-1`：

```json
{
  "outline_ref": "exact approved outline ref",
  "candidate_id": "exact current draft candidate id",
  "order_requirement": "chronological | causal | progressive | none",
  "required_items": [
    {
      "node_id": "section_dilemma",
      "kind": "section",
      "section_id": "dilemma",
      "title": "困境章节",
      "outline_position": 1
    },
    {
      "node_id": "manual_work",
      "kind": "content",
      "section_id": "dilemma",
      "outline_coverage_index": 0,
      "evidence_refs": [{
        "source_id": "src_a",
        "transcript_version_id": "tr_a_1",
        "segment_id": "seg_1",
        "start_ticks": 100,
        "end_ticks": 200
      }],
      "precedence_position": 0
    }
  ],
  "mapping": [{
    "node_id": "manual_work",
    "kind": "content",
    "section_id": "dilemma",
    "draft_block_ids": ["body_a"],
    "draft_position": [2, 0]
  }],
  "precedence_checks": [{
    "before_node_id": "manual_work",
    "after_node_id": "machine_work",
    "before_position": [2, 0],
    "after_position": [3, 0],
    "result": "PASS"
  }],
  "missing_required_nodes": [],
  "result": "PASS | FAIL | NOT_APPLICABLE"
}
```

section node 的 `section_id` 来自批准 Outline，映射使用其唯一 title 与当前 Draft 独立
`section_title` heading（schema 2；schema 1 readback 的确定性 heading projection 也同样处理）。
没有 heading 是 `missing_required_node`，同名 heading 多于一个是 `mapping_ambiguous`；不按 occurrence、
block 文本或语义猜测。content node 不匹配标题或 `canonical_text`，只在其 section heading range 内扫描当前
有序 `source_excerpt.refs` 和已录音 narration `recorded_refs` 的 media-ref stream。每个 ref 必须按完整
五元组 `(source_id, transcript_version_id, segment_id, start_ticks, end_ticks)` 精确相等，并且 node 的
ref 列表必须作为一个连续 subsequence 出现；可以跨没有 heading boundary 的相邻 Draft blocks，映射记录
所有触及 block IDs 以及第一个 ref 的 `[block_index, ref_index]`。中间出现另一条 media ref、跳过 ref、
跨 heading、未录音 narration 或不存在候选均是 `missing_required_node`；同一 exact sequence/ref 出现
两次或以上是 `mapping_ambiguous`。多候选和零候选都 FAIL，不猜、不合并、不自动重排。

映射后只对显式 required content nodes 按 `precedence_position` 组成线性序列，逐对比较相邻节点的
`draft_position`；不建 generic causal graph/DAG，不调用 LLM judge，也不增加 Core/workflow gate。section node 只建立 heading
映射，不凭章节顺序制造 content precedence。无关 block（含其自身的其他内容）可插入而不改变已满足的
相邻 precedence。A→B→C 必须 PASS，A→C→B 必须 FAIL；causal A→B 必须 PASS，progressive 保持顺序必须
PASS。相同 Outline/candidate/blocks 的重复 readback 必须得到字节稳定、顺序稳定的 checklist；candidate
stale 或 current blocks 改变后必须丢弃旧 mapping，以新的 exact candidate 重新读取并重建。
FAIL 时明确列出逆序、`missing_required_node` 或 `mapping_ambiguous` 证据，不调用自动修复。

最小回归向量固定为：

| 场景 | checklist result |
| --- | --- |
| 同章 chronological A→B→C | `PASS` |
| 同章 chronological A→C→B | `FAIL` |
| causal A→B | `PASS` |
| progressive 保持顺序 | `PASS` |
| 同章 A→无关 block→B→C | `PASS` |
| 同一 content node 跨两个相邻 blocks 且 refs 连续 | 唯一 mapping |
| exact ref 缺失 | `FAIL` + `missing_required_node` |
| exact ref/sequence 重复 | `FAIL` + `mapping_ambiguous` |
| no explicit order | `NOT_APPLICABLE` |
| same Outline/candidate/block readback | deterministic identical |
剪辑要求确认后、生成初稿前，必须建立一份至少含一个主体章节的正式大纲。根据用户对结构的
表达，区分以下三种行为语义；它们是 Agent policy，不写入 Core schema：

- `agent_propose`：用户没有提供结构，或明确要求 Agent 设计结构。读取完整授权 Transcript 后提出
  大纲；普通采访类 `interview-v1` 通常可建议约 4–7 个主体章节，但这只是默认编辑建议，不是要求。
  完整展示大纲并等待用户确认；用户可以确认、重排、增删章节、改变开头或结尾、要求另一版，或回复
  “按推荐方案出稿”。不得自动调用 `approve_outline`。
- `user_reference`：用户说“参考这个提纲”“可以根据素材调整”“大体按这个思路”等。Agent 可以结合
  真实 Transcript 调整结构，但必须明确回报主要结构变化，展示最终大纲并等待用户确认；reference
  不是 approval，不得自动调用 `approve_outline`。
- `user_directed`：用户说“就按这个提纲出初稿”“严格按照这个结构”“不要重新设计大纲”“按我这个
  提纲组稿”等，已明确指定 exact structure。Agent 把它机械正规化为正式大纲，保留用户章节数量与
  顺序；不得把 3 章拆成 4 章，也不得把 9 章合并到 7 章。如果正规化没有实质改变用户结构，这次明确
  指令本身就是该 exact structure 的既有 approval semantics：调用 `submit_outline` 后立即针对返回的
  exact `outline_ref` 调用 `approve_outline`，不再询问“请确认这个大纲”，然后生成 Draft。仍需状态
  回报“已按你提供的提纲建立正式结构，章节数量和顺序未修改”，以及任何素材缺口；这不是第二次
  confirmation gate，也绝不同时批准初稿、采用粗剪或正式导出。

机械正规化只包括：生成安全 `section_id`、把自然语言标题映射成正式 section、补齐 Core 必需字段、
依据已确认总目标时长填写合理 section target duration、绑定 exact evidence refs、计算 coverage、填写
narration status、Unicode/newline normalization，以及不改变编辑意图的 deterministic metadata。以下均为
实质变化：增加、删除、合并、拆分或重排主体章节；改变某章核心主题/职责；改变用户指定的 opening/
ending；因素材不足主动改变用户设计的内容逻辑。发生实质变化时，必须展示变化并等待用户确认后才
调用 `approve_outline`；原始 `user_directed` 不是无限调整授权。

`user_directed` 某章素材不足时仍保留该章，标记当前可靠素材不足，尽量列出已有 exact evidence，并在
Draft 前或生成过程中报告缺口；不得删除/合并该章、虚构内容或机械重复素材。如果建议改变结构，回到
展示变化并等待确认。大纲始终包含简短标题候选、开头、主体章节、结尾、大致时长分配、必须保留内容
覆盖和解说状态。“金句快剪”只确认形式；候选金句必须来自真实 exact refs，可反复替换，不得自动定稿
或虚构。

需要等待确认的 `agent_propose`、`user_reference` 或实质修改版本，每次新建或修改后必须主动回显完整
当前版本：总标题、简介或整体表达说明、开头、每个章节标题与内容简介、每章“目标时长”、结尾、解说
状态、总目标时长和允许范围。必须明确：“确认后仍可返回修改提纲；修改会创建新版本，不会覆盖历史
版本。”`user_directed` 无实质变化时同样完整回报最终结构和缺口，但不重复索取确认。

初稿页面用普通语言说明：左侧是初稿；右侧可查看和搜索全部素材的原始转录；左侧选区可删除或移到
目标光标，右侧选区可加入目标光标。初稿确认只确认内容与顺序，不等于采用粗剪或批准正式导出。
初稿确认且所有内容可播放后，用户可选择“生成粗剪预览”；在预览检查后再选择
“采用这个粗剪版本”。粗剪页只负责 Virtual Timeline 连续播放、文稿定位、返回初稿
和采用；任何内容、顺序或剪点问题都返回初稿修改后重新生成预览。

来源同期声原话不得改写、漏词或补写。解说必须明确标为解说；未录解说可以确认文字，
但不能生成粗剪，必须作为新素材导入、转录、校正并绑定后才可播放。

## 仅供 Agent 执行的顺序

Gate 阻塞诊断遵循 `roughcut-basics` 总原则。
以下工具名是内部执行链；即使底层存在多个确认对象，也不要把它们作为用户导航名称。
多机位任务先收集并向用户逐项回读主机位、副机位分组和素材授权；这次分组确认只保存
授权，不调用对轨。副机位虽已导入 Project，但不得进入 Brief、Outline、Draft 或内容决定；完成第 11 步的 exact adopt 后，必须再次确认同一授权仍有效。

1. 调用 `workflow_status`，只使用它返回的 current confirmation basis、presented
   subject、allowed action 和 exact bindings；再调用 `project_open` 读取当前 revision。
   对每个选定
   Source 调用 `transcript_versions_read`，只绑定其精确活动
   `transcript_version_id`；发现活动 Edit stale 时停止并报告，绝不改写冻结引用。
2. 以精确有序 `source_bindings` 调用 `readable_transcript_read`。从 offset 0 分页，直到
   没有下一页；每页必须保持同一 revision、bindings 与 view hash，只要 `next_offset`
   非空就不得 `submit_outline`/`submit_draft`。Agent 必须覆盖完整 Readable Transcript，
   但不要求用户逐页通读。过滤只帮助讨论，不能代替完整覆盖。
3. 用户引用某段原话时，用 `transcript_selection_resolve` 传入同一 bindings、view hash、
   occurrence 和 exact offsets；重复文字不得默认第一处。无可信 fine units 时接受工具
   返回的完整 segment 扩展，绝不估算时码。用户需要文件时可用 `markdown_export` 导出，
   但不把 Markdown 当作项目回写输入。所有候选依据都必须保留
   source/transcript/segment/ticks。
4. 在完整 Readable Transcript 覆盖后，向用户回读剪辑要求摘要，只整理用户输入、
   Agent 建议项和会影响可行性的事实缺口，不在这里提前提出开头、主体、结尾或大纲。
   Brief theme 必须保存用户最终确认的具体内容主题；成片类型单独保存为作品类型，不能用类型代替主题。
   用户明确确认剪辑要求摘要后，重新调用 `workflow_status`，回传 core 给出的当前剪辑
   要求确认依据和完整用户业务字段，只调用一次 `workflow_action(confirm_brief)`。
   `eligible_speaker_waivers` 非空但用户尚未明确决定时，停止并向用户说明，不得调用
   `confirm_brief`；用户明确选择 waive 时，`speaker_resolution_waivers` 只传同一次 status
   返回的 eligible 项，保持其原始顺序，不得排序、按 local speaker ID 重建、去重或混入旧
   status 条目；用户明确选择不 waive 时继续 speaker mapping，在 eligible 仍非空期间同样
   不得调用 `confirm_brief`；只有当前 status 的 eligible 已为空时，调用 `confirm_brief`
   才传空数组。Agent 不得自行豁免。
   保留 action ID；动作后读取同一 receipt/status，不再调用 `brief_create` 做第二次保存。
   若发生 stale，停止并从新状态重新开始，不用旧输入重试。
5. 单素材调用 `agent_context`；多素材调用 `multi_source_context`，均传入精确 active
   bindings、Brief 和同一 revision。从 offset 0 分页直到没有下一页，每页保持同一
   revision、bindings 和 `context_hash`，只要 `next_offset` 非空就不得
   `submit_outline`/`submit_draft`。只使用返回的 refs、canonical text、ticks 和 Person
   信息。禁止直接读取 `transcripts/*.json` 替代 canonical 工具（覆盖 readable 和 context 两阶段）。
6. 完整分页读取 Readable Transcript 后，按上面的 `agent_propose` / `user_reference` /
   `user_directed` 语义建立正式大纲。普通 interview-v1 的 Agent 自主建议通常约 4–7 个主体章节；
   用户自带或内容需要的结构只要求非空，不受该建议限制。不得因单素材或 Agent 认为顺序明确而省略
   formal Outline artifact。金句快剪只确认形式；候选金句必须来自真实 exact refs，允许用户反复替换，
   不得自动定稿或虚构。缺少可靠金句、结尾或过渡时必须明确说明；缺少章节素材时也必须报告，
   不得补写或改写同期声；`user_directed` 的缺素材章节保持存在。
   如果用户明确声明同章内容链（例如“传统手工 → 潍坊机器生产 → 价格低 → 冲击徐州手工 → 结果”），在
   Outline 阶段按用户声明的线性顺序列出这些 content nodes，并把每一项的 exact evidence refs 分别放入
   已有 `required_content_coverage` entry；只保留 entry index 作为批准后临时 checklist 的 provenance，不把
   node metadata 写进 schema 1。不能为没有用户声明或没有 exact refs 的节点补写自然语言标识。
   大纲本身不创建 Proposal、Decision 或 Render。
   先用完整 closed snapshot 调用 `workflow_action(submit_outline)`；向用户展示 core
   返回的 exact current 大纲后，保留 `outline_ref`。`agent_propose` 必须展示完整大纲并等待用户明确确认；
   `user_reference` 若有调整，必须
   展示主要变化和最终大纲并等待确认。二者确认后重新读取 `workflow_status`，再调用
   `workflow_action(approve_outline)`。`user_directed` 无实质结构变化时，用户原指令已经批准同一结构：
   完整状态回报后直接以新的 action ID 调用 `workflow_action(approve_outline)`，不得再问“请确认这个
   大纲”；若有实质变化则仍展示变化并等待确认。`submit_outline` 与 `approve_outline` 始终使用不同且
   保留的 action ID；这次 directed approval 只跨 Outline gate，不得跨初稿确认、粗剪采用或正式导出。
7. 大纲批准后，保留用户明确的 `order_requirement`（未明确则固定为 `none`），才使用完整有序 blocks、精确 bindings、Brief ref 与 `context_hash`
   调用 `workflow_action(submit_draft)`，然后调用 `content_draft_read` 读回完整候选和
   `duration_acceptance`。在向用户回读或调用 `workflow_action(approve_draft)` 前，必须以当前 approved
   Outline 的 `required_content_coverage.evidence_refs` 重建上面的临时 checklist，并建立 section/content
   node → Draft block/ref mapping、检查 precedence；`PASS` 才能声称显式顺序已保留，`NOT_APPLICABLE` 才表示
   没有顺序约束。`FAIL`、`missing_required_node` 或 `mapping_ambiguous` 时停止验收回读，明确报告 exact
   refs/位置证据并回到现有初稿改稿路径，不自动重排、不改变 workflow 状态。写入
   `display_title`，并为各章节写入 `section_title`；Source excerpt 只保留 exact refs，
   正常 schema 2 Agent 提交的 `source_excerpt` MUST OMIT `canonical_text`，由 Core 从当前 Transcript
   exact refs 派生；只有明确标记为 legacy compatibility 的 caller 才能提供该字段，且必须严格相等，
   不得由 Agent join、重写或维护副本。解说保持 editorial。组装每个 `source_excerpt` block 时，必须只放同一
   Source/Transcript 上按上下文顺序连续的一组 exact refs；跳过一个真实 Transcript segment
   就结束当前 block，从下一个采用 ref 新建 block，切换 Source 或 Transcript 也必须新建 block。
   不伪造中间 ref，不改写 canonical text 掩盖跳段；`section_title` 与 `source_excerpt` 是独立 block。
   回归例：原始顺序为 `seg_1、seg_2、seg_3`，采用 `seg_1` 和 `seg_3`、排除 `seg_2`，必须生成
   两个 `source_excerpt` blocks，而不是一个包含 `seg_1+seg_3` 的 block。一次组装仍不增加
   preflight API 或用户确认步骤；core 拒绝时停止并报告精确错误，不自动重放或修改 Project JSON。
   `workflow_action(submit_draft)` 是 state-changing workflow action，不是 validator。失败后立即进入“只读诊断阶段”。
   根因未确定前，严格禁止新的 state-changing action、换 `action_id`、缩 payload、构造 dummy/minimal/single-block
   candidate、通过“先写进去看看”探测，或直接修改 Project JSON、改变业务对象；诊断阶段后续动作仅限
   `workflow_status`、`project_open`、现有 read-only 工具和保存并分析 exact error。只读重算 hash、重新读取 current
   status、正确 offset 继续分页等不改变业务事实的机械纠错，各只允许一次。
   只读证据确定根因后，若是业务意图不变的安全机械纠错，允许一次正式恢复动作；若涉及新的用户决定，必须先说明
   根因与影响并取得明确决定，再允许一次正式恢复动作。恢复动作是正式执行，不得继续被当 validator；若只读证据
   不能证明安全则停止并报告。保留现有合法 scope reapproval 语义。
8. 打开初稿页时，把 `roughcut review` 作为由当前宿主托管的前台长生命周期进程启动，
   保存进程或终端 session 句柄，不能在交付 URL 后结束该执行单元。向用户交付地址前，
   必须同时满足同一进程仍存活、`127.0.0.1` loopback 端口监听、首页 HTTP 200 与
   `/api/workflow/draft-editor` HTTP 200，四项均通过才允许报告页面已打开/可访问；不得仅凭
   startup JSON/URL 报告。长文稿首次载入可以显示加载状态，不能把仍在
   组装 Snapshot 误报为服务退出。用户明确完成、取消或要求停止后才关闭进程，并确认
   端口清理。若交付前或用户操作中服务意外退出，只使用同一 bindings 和不可变 draft
   重新启动并给出新 URL；不得重建初稿、重跑转录或把旧 URL 继续交给用户。
   WorkflowRun 驱动的启动必须使用 `roughcut review <project> --run-id <run_id>`；该入口只读取
   指定的持久 run 并使用其 current exact bindings/candidate，禁止按 latest 或聊天记忆猜测。
9. 向用户展示完整初稿后，只有获得该 exact candidate 的明确确认才重新读取
   `workflow_status` 并调用 `workflow_action(approve_draft)`。该固定 action 同时生成
   confirmed child 和待审阅粗剪候选；动作后只读取 receipt/status，不再调用
   `content_draft_confirm` 或 `content_draft_propose`。初稿页按钮必须走同一 façade。
10. 未录解说时停止：不得省略、创建 placeholder 或调用低层 Proposal 写入口。只有
   current、完全媒体化的初稿才能提交 `approve_draft`。
11. 只能使用服务端返回的精确 Proposal/Review snapshot 打开“粗剪预览与采用”；不得由
   Agent 或浏览器重建 clips。可见总时长、进度、seek 和结束位置必须来自 Virtual
   Timeline，不能使用当前 Source 的原生时长。用户发现问题时返回初稿；返回动作本身不
   改内容或 revision，首次实际编辑才创建不可变 child，之后必须重新确认并生成新预览。
   用户明确“采用这个粗剪版本”后，重新读取 `workflow_status` 的 exact Proposal ref，
   只调用 `workflow_action(adopt_roughcut)`，然后读取 receipt/status。不得再调用
   Proposal confirm 低层入口，也不得把采用解释为正式导出批准。
12. 多机位任务只有在第 11 步 exact adopt 成功且分组授权仍有效后，才由 Host 预持有新的
   alignment operation ID 与 artifact ID，调用 `align_multicam`；随后只用同一 operation ID
   调用 `media_operation_status`，读取 exact succeeded result ref 和 core coverage。失败、
   interrupted、result ref 不一致或授权 stale 时停止，不把对轨迁移给 `render-roughcut`，
   不扫描 artifact/staging，也不自动重试。把 exact succeeded alignment ref 与 core coverage
   交给后续正式导出流程；本 Skill 不生成平行 MP4。`main_camera.camera_id` 必须精确使用协议 ID
   `"main"`，不得用文件夹名、相机型号或用户显示名代替；每个副机位使用唯一且不等于 `"main"` 的 ID。
   用户已明确给出文件对应关系时，用该副机位组内 closed `source_pairs`
   （每项恰好 `main_source_id` + `auxiliary_source_id`）原样转达，core 只执行列出的 pairs；
   未给出 pairs 且组内不是恰好单 main + 单 aux 时停止询问用户，不按下标、数量或文件名猜配对，
   也不搜索组外素材。
13. 任一 revision、context、binding 或 Brief stale 时，停止并重新读取；不自动重试旧
   写入、合并或覆盖。

优先使用 `references/tool-contract.md` 描述的本地 stdio MCP 工具。只有宿主没有 MCP 且
安装诊断已经返回绝对 CLI 路径时，才使用该绝对路径执行 `<command> --json`；不要搜索
PATH 或源码来定位命令。
