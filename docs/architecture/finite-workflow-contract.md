# Finite Workflow Contract


配套 vectors：`core/tests/fixtures/finite-workflow-vectors.json`

## 1. 目的、权威性与实现边界

本文冻结 Roughcut 首版有限工作流控制层。它只回答以下问题：

- 当前 Project 是否存在一条可写的普通工作流；
- 当前可接受哪个固定用户动作；
- 用户批准绑定了哪个精确对象和哪些精确依赖；
- 一个动作如何只提交一次固定 intent transaction、至多跨一个业务门并留下一个可回读
  receipt；
- 直接调用既有 CLI/MCP 写入口时，core 如何阻止无 run、越级和错对象。

本文是 `docs/spec.md`“有限工作流控制层”的 schema 1 细化契约。若摘要与本文冲突，
本阶段先修正文档并复核，不由实现自行选择。现有
`docs/agent-tool-contract.md` 的 tool schema version 22 和已发布工具语义保持不变；
本文出现的 `workflow_start`、`workflow_status`、`workflow_action`、
`workflow_cancel` 全部是 **proposed**，本轮不公开、不接 CLI/MCP。
集中任务阶段与恢复升级路线见
当前实现以此契约与公开工具接口为准。

第二阶段 Python domain/store、统一项目锁与有限自动回退已经主评审通过，但尚未接
Review server/UI、长任务记录、ASR、Proxy、Render 或公开 CLI/MCP；当前仍不具有生产
门控保护。本轮只补齐第三阶段实施所需的 façade、closed input 与 prepare/publish 契约，
不实施生产代码。

## 2. 固定术语

- **Project**：现有 schema 1 `project.json` 及其受控目录。WorkflowRun 不替代它。
- **run**：一个 Project 的一次普通用户工作流。首版同一 Project 最多一个
  `lifecycle=active` 的 run。
- **subject**：用户本次动作明确确认或提交的精确对象。
- **content hash**：subject 自身的规范化内容摘要。
- **dependency bundle**：某道门手写、封闭的依赖字段集合。
- **dependency hash**：dependency bundle 的规范化内容摘要。
- **intent transaction**：一个固定用户意图在契约中手写列出的有限领域写集合。
  它可以发布多个彼此不可分割的 artifact，但不能跨越第二个用户确认门。
- **transition**：一次固定的 stage 变化。一次 action 最多发生一次。
- **receipt**：以 `action_id` 为幂等键的持久成功回执。
- **effective approval status**：依据不可变批准记录和当前 dependency 重算得到的
  `current` 或 `stale`；不另存可漂移状态。

## 3. 固定 stage、lifecycle 与系统迁移

### 3.1 枚举

`stage` 只允许：

1. `scope_review`
2. `outline_review`
3. `draft_review`
4. `roughcut_review`
5. `export_review`
6. `exporting`

`lifecycle` 只允许：

- `active`
- `completed`
- `canceled`

后台 ASR、人物对应、Brief 收集、Proxy 和长操作不是 stage。

### 3.2 正常主线

```text
scope_review
  --submit_outline--> outline_review
  --approve_outline--> draft_review
  --approve_draft--> roughcut_review
  --adopt_roughcut--> export_review
  --approve_export--> exporting
  --export_succeeded--> exporting/completed
```

`return_to_draft` 只允许：

```text
roughcut_review -> draft_review
export_review   -> draft_review
```

首个 Decision 前的显式上游修订只复用既有 action：

```text
outline_review --approve_scope--> scope_review
draft_review   --approve_scope--> scope_review
draft_review   --confirm_brief--> scope_review
```

它们各自仍只跨一个业务门；不会自动重新确认 Brief、Outline 或 Draft。

正式导出终态是系统结果，不是第二个用户 action：

- exact render operation 成功：`exporting/active -> exporting/completed`；
- exact render operation 失败或中断：
  `exporting/active -> export_review/active`，本次 action 不发布成功 ApprovalRecord 或
  receipt；必须用新 `action_id` 重新展示当前输出摘要并批准；
- `workflow_cancel`：任一 `active` run 只变为相同 stage 的 `canceled`，不得修改或删除
  领域 artifact；
- `completed`、`canceled` run 永远只读。

长任务记录尚未实现时，受控 façade 可以同步执行现有 Render，但必须产生与上述状态相同的
最终结果；不得把 Host task/PID 当作导出成功。

## 4. 存储、路径与 containment

### 4.1 固定相对路径

所有路径都由 core 根据安全 ID 构造，调用方不得提交 artifact 路径：

```text
<project>/workflow/runs/<run_id>.json
<project>/workflow/approvals/<approval_id>.json
<project>/workflow/receipts/<action_id>.json
<project>/workflow/transactions/<action_id>.json
<project>/workflow/export-staging/.claim.lock
<project>/workflow/export-staging/<staging_id>/owner.json
<project>/.roughcut-project.lock
```

`run_id`、`approval_id` 和 `action_id` 必须匹配
`^[A-Za-z0-9_-]{1,128}$`。JSON 中只保存上述 project-relative ref 或领域 ID；
不得保存绝对 Project root、Source locator、临时目录、Host token 或媒体正文。

### 4.2 containment

每次读写在取得项目锁后执行以下检查：

1. `project.json` 经现有 `ProjectStore` 读取并得到 `project_id`；
2. Project root 只解析一次；`workflow` 到目标文件的每一级现存节点都不得是 symlink；
3. 目录必须是目录，JSON/lock 文件必须是普通文件；发现 hard-link 异常时拒绝；
4. 规范化目标必须仍在 Project root 内；
5. 文件名中的 ID 必须等于 payload ID；
6. run/approval/receipt 的 `project_id` 必须等于当前 Project；
7. approval 的 `run_id` 必须等于当前 active run；receipt 必须等于该 action 提交后的
   exact run，只有 `approve_export` 可对应 completed、`workflow_cancel` 可对应 canceled；
8. 未知 schema、损坏 JSON、重复 JSON key 或额外顶层字段一律拒绝，不做猜测迁移。

稳定错误为 `workflow_integrity_error`。不得跟随可疑路径后再尝试修复。

### 4.3 单 active run

在同一项目锁内枚举并校验 `workflow/runs/*.json`：

- 零个 active run：普通受控写入口返回 `workflow_required`；
- 一个 active run：只有它可写；
- 两个或更多 active run：所有工作流写入返回 `workflow_run_conflict`；
- completed/canceled run 只读；
- `workflow_start` 只有在零个 active run 时成功；
- 历史 run 不支持 fork、恢复 checkpoint 或 time travel。

run 目录不存在表示旧 Project，不表示损坏。

## 5. WorkflowRun schema 1

### 5.1 封闭 JSON 形状

以下字段全部必需；nullable 字段必须显式为 `null`，不得省略：

```json
{
  "schema_version": 1,
  "run_id": "wfr_...",
  "project_id": "proj_...",
  "stage": "scope_review",
  "lifecycle": "active",
  "created_at": "RFC3339 UTC",
  "updated_at": "RFC3339 UTC",
  "ordered_bindings": [
    {
      "source_id": "src_...",
      "transcript_version_id": null,
      "transcript_content_hash": null
    }
  ],
  "scope_authorizations": [
    {
      "source_id": "src_...",
      "transcribe": true,
      "speaker_diarization": true
    }
  ],
  "artifact_refs": {
    "brief": null,
    "outline": null,
    "content_draft": null,
    "proposal": null,
    "decision": null,
    "render": null
  },
  "readiness_basis": {
    "scope_subject_hash": null,
    "brief_subject_hash": null,
    "required_transcripts": [],
    "speaker_resolution": {
      "mode": "not_ready",
      "refs": [],
      "waiver_subject_hash": null
    },
    "blocking_operation_ids": []
  },
  "approval_refs": {
    "scope": null,
    "brief": null,
    "outline": null,
    "draft": null,
    "roughcut": null,
    "export": null
  },
  "last_receipt_ref": null
}
```

对象均为 closed schema，不接受未定义字段。

### 5.2 ordered bindings 与 scope authorizations

- 数组顺序有语义，hash 时不得排序。
- 同一 `source_id` 在数组中只能出现一次。
- `scope_review` 且 scope 尚未批准时允许空数组；这是新 Project 先显式 start、再用现有
  `source_add` 建立候选的唯一空状态。
- core-owned scope basis 同时冻结两组不同事实：`current_run_scope` 是 run 当前
  ordered bindings/authorizations；`selectable_project_sources` 是锁内当前 Project 中全部
  可选择 Source 的 snapshot，按 Project 持久顺序排列。调用方只回传 basis，并用完整有序
  `source_authorizations` 明确目标 scope；Host/Agent 不计算 hash，也不能把 Project 中未出
  现在 basis 的 Source 加入目标。
- 首个 Decision 前，无论首次批准或 reapproval，target 都可以从
  `selectable_project_sources` 明确选择任意非空、有序、去重子集；不要求包含 current run
  scope 的全部 source IDs。批准后，`source_add` 只增加 Project selectable Source，不
  静默改变 run。
- **首个 Decision 前显式 reapproval**：在 `scope_review/outline_review/draft_review`
  可以复用 `approve_scope`。目标可以增加、删除、替换或重排 Source，也可修改逐素材授权；
  替换恰好等于从 target 删除旧 Source 并加入一个 selectable 新 Source。source/order/
  authorization 至少一项必须变化。成功后 target 完整替换 run bindings/authorizations；
  这不是隐式 binding sync，也不新增 workflow action。每个 target entry 若有经验证
  active Transcript 就写其 exact ID/hash，否则写两个 null 并要求本次
  `transcribe=true`。
- “首个 Decision 已发布”由当前 run 任一成功 `adopt_roughcut` receipt 及其 immutable
  Decision ancestry 派生；即使 `return_to_draft` 已清 current Decision ref，该事实仍为
  true。此后 target 只要增加、删除、替换或重排 binding 就
  `workflow_transition_not_allowed`；不得用新的 scope basis 或清空 current ref 绕过。
- run 开始及素材批准时，尚未转录的 entry 使用两个 `null`。
- `approve_scope` 必须把数组冻结为非空、用户确认的 ordered Source 集合；其后不得用
  `source_add` 静默扩张当前 scope。
- 每个 scope binding 在 `approve_scope` 时必须满足二选一：当前 Project 已有经完整
  schema、ownership、active pointer 和 content hash 校验的 exact active Transcript；
  或该 source 的本次 `transcribe=true`。两者都不满足时返回 `workflow_not_ready`，
  detail 固定为 `scope_binding_without_active_transcript_or_transcribe`，不得因聊天、
  已存在但非 active 的 Transcript 或 Host 猜测而放行。
- 已批准 `transcribe=true` 的转录完成、既有 `transcript_correct` 创建并激活 immutable
  child，或用户通过 `transcript_version_activate` 显式切换 active Transcript 后，只同步同一
  `source_id` entry 的 exact `transcript_version_id/content_hash`。同步不改变 source 顺序、
  授权、stage、lifecycle、artifact refs 或 approval refs，不签发批准，也不把校正或显式激活
  推断为新的转录或 speaker 授权。
- Project active Transcript 是这次单素材同步的提交事实。若 Project 已提交而 run replace
  前进程退出，下一次 `workflow_status` 在同一 Project write lock 内先完整验证 Project
  active Transcript，再按 binding 顺序原子修复所有发生该确定性裂缝的 exact entries；
  run 写入前再次中断时下次重复，写入后再次调用为 no-op。修复失败不得返回半同步
  Project/run，固定错误为 `workflow_binding_sync_failed`，责任主体写明
  “Roughcut workflow façade 未能同步已验证的 active Transcript binding”。
- 这条同步不是 workflow action、approval、TransactionMarker、stage transition 或
  OperationRecord。它只能把 run 对同一 Source 的缓存 binding 收敛到已提交 Project
  active Transcript；不能选择 latest file、扫描目录猜版本、扩张 scope 或自动继续
  outline/draft。只同步 `lifecycle=active` 的唯一 run；completed/canceled 历史保持只读。
- 所有 entry 非 null 后，数组必须逐项等于现有 application service 使用的
  `source_bindings`；裸 `segment_id` 永远不足以定位内容。
- Transcript correction/activation 不迁移历史下游批准；它更新该 entry，并按固定 bundle
  使受影响批准 stale。
- 增加、删除、替换或重排 Source 是 scope 变化，只能走上述显式 reapproval；首个
  Decision 后若需改变 scope，必须另立重新基线任务，不在本 run 改变 bindings。
- `scope_authorizations` 是唯一持久化的用户授权选择，不复制 Source metadata、
  fingerprint、locator 或输出设置；这些事实仍从当前 Project 重建。
- scope 未批准时该数组可以为空；`approve_scope` 后必须与 `ordered_bindings` 逐项同序、
  source ID 完全相同。每项只含 closed boolean `transcribe` 与
  `speaker_diarization`，后者为 true 时前者也必须为 true。
- 后续 `workflow_status` 和受控 ASR 写入口从该数组读取授权，不能从聊天、Skill、
  Transcript 是否已存在或 Host 临时状态推断。

reapproval 从 `outline_review/draft_review` 成功时固定返回 `scope_review`。新 scope
approval 替换 current scope approval；Brief/Outline/Draft/Roughcut/Export approval refs
全部清为 null，Brief/Outline/Proposal/Decision/Render current artifact refs 清为 null，
readiness basis 重新派生。历史 approval/artifact 文件不删除、不改写。若原来存在 Draft
anchor，`artifact_refs.content_draft` **只作为 exact rebase anchor 保留**；它不再表示
current-approved Draft。第一个合法 rebase child 的 `submit_draft` receipt 成功发布时，
该字段原子切换为该 unconfirmed child；receipt 前失败或崩溃则恢复 exact old anchor。该
暂存语义不增加 schema。

### 5.3 artifact refs

每个非 null ref 的固定形状为：

```json
{
  "artifact_id": "domain id",
  "schema_version": 1,
  "content_hash": "64 lowercase hex"
}
```

例外：

- `outline` 使用 `artifact_id` 等于 `outline_<content-hash 前 16 位>`，并额外保存
  `snapshot`；它仍是 run 内的 canonical snapshot，不新增 Outline 领域对象；
- schema 2 Proposal/Decision 的 `schema_version` 为 2；
- `render` 接受既有 Render Plan schema 1/2；`artifact_id` 是 render ID，
  `content_hash` 绑定 actual Render Plan 完整 payload 的 canonical hash。单素材
  `RenderPlan` 使用 schema 1，多素材 `MultiSourceRenderPlan` 使用 schema 2；
- `content_draft` ref 是当前 Draft **workflow anchor**。首次 `submit_draft` 时从 `null`
  设为该 prepared unconfirmed Draft；其后只接受通过逐 parent ref 验证、同 Project/
  run/bindings/context 且属于当前 anchor 的不可变后代。目录顺序、mtime 和词法 ID
  永远不能决定 “latest”；
- scope/Brief reapproval 后，该 ref 暂时保留 exact old anchor。第一个合法 rebase
  `submit_draft` 的逻辑提交边界同时发布 candidate、把 anchor 切为 prepared unconfirmed
  rebase child 并发布 receipt；只有 receipt 已成功发布时新 anchor 才是 committed 状态。
  普通同 basis 后续 child、Review mechanical child 和 sibling 都不自动替换 anchor；
- `approve_draft` 成功时 anchor 切换为 core 在 marker 前准备的 confirmed child；
  `return_to_draft` 保留该 confirmed anchor，后续分支必须从它或其经验证后代显式提交；
- Render manifest 不由 `artifact_refs.render` 保存或合并进其 hash；它继续作为
  `approve_export` receipt 的独立 `manifest` output ref，单/多素材分别使用既有
  manifest schema 2/3 和各自的 exact content hash；
- ref 只指当前 run 候选。返回初稿时，Proposal/Decision/Render ref 置 `null`，旧文件仍是
  Project 历史，不删除、不覆盖。

Outline snapshot schema 1 固定为：

```json
{
  "schema_version": 1,
  "title": "short title",
  "opening": "opening plan",
  "sections": [
    {
      "section_id": "section_...",
      "title": "section title",
      "summary": "content plan",
      "target_duration_ticks": 120000
    }
  ],
  "ending": "ending plan",
  "required_content_coverage": [
    {
      "requirement": "exact user requirement text",
      "covered": true,
      "evidence_refs": [
        {
          "source_id": "src_...",
          "transcript_version_id": "tr_...",
          "segment_id": "seg_...",
          "start_ticks": 0,
          "end_ticks": 120000
        }
      ]
    }
  ],
  "narration_status": "none"
}
```

`sections` 必须是非空、有序数组；`section_id` 唯一；duration 为正整数；`narration_status` 只允许
`none/pending/to_write/recorded`。章节数量属于 workflow/editorial policy，不是 workflow integrity；
Core 不设置节目章节数量上限。`covered=false` 时 `evidence_refs` 必须为空；
`covered=true` 时必须非空并通过 exact binding/range 校验。第一次生成 Draft 时，
`display_title` 和独立 `section_title` heading blocks 必须以 current approved Outline 为基础；
这不是所有 immutable descendants 的永久字段锁。第 8.5 节后续 scoped/full revision
可以按用户明确范围调整结构 metadata，未点名 blocks 仍逐字段不变。

### 5.4 readiness basis 与派生 readiness

run 只持久化上面列出的精确 basis。每次 status/action 都从当前 Project、artifact 和未来
OperationRecord 重算以下响应字段：

```json
{
  "scope_approved": false,
  "brief_approved": false,
  "required_transcripts_ready": false,
  "speaker_resolution_ready_or_waived": false,
  "blocking_operation_ids": []
}
```

规则：

- `scope_approved`：scope approval ref 存在、未 stale，且其 subject hash 等于
  `readiness_basis.scope_subject_hash`；
- `brief_approved`：Brief approval ref 存在、未 stale，active Brief 的 ID/schema/content
  hash 等于 run ref；
- `required_transcripts_ready`：每个 ordered binding 都非 null，Source/Transcript
  ownership、active binding、artifact schema 和 content hash 全部一致；
- `speaker_resolution_ready_or_waived`：
  - bound Transcript 没有 local speaker：`mode=no_speakers`；
  - 每个出现的 local speaker 都有 exact
    `(source_id, transcript_version_id, local_speaker_id, person_id)` 用户确认：
    `mode=all_mapped`；
  - 用户在 `confirm_brief` 中对列出的 exact local-speaker refs 明确留空：
    `mode=waived`，并保存 waiver subject hash；
  - 其他情况为 `not_ready`；
- `blocking_operation_ids`：只包含当前门明确需要且状态为 pending/running 的 exact
  operation ID，稳定按创建顺序返回。

Proxy 的固定规则：

- 原素材能被 Review 正常读取、seek 和播放时，Proxy 缺失、失败或正在生成都不进入
  `blocking_operation_ids`；
- Proxy 永不阻塞 `submit_outline`、`approve_outline`、`submit_draft` 或正式 Render；
- 只有现有 Review 媒体检查已经证明某个 bound Source 不能直接审阅，且用户明确批准为该
  exact Source/profile 生成 Proxy 时，该 operation 才阻塞进入可播放粗剪；
- 正式 Render 始终以注册原素材和 fingerprint 为依赖，不以 Proxy 为依赖。

`allowed_actions` 与 `next_action` 只在 `workflow_start/status/action/cancel` 成功响应中按
固定表派生，绝不写入 run、approval、receipt 或 Project。

### 5.5 readiness/approval/receipt ref 形状

`readiness_basis.required_transcripts` 的每项恰好为：

```json
{
  "source_id": "src_...",
  "transcript_version_id": "tr_...",
  "schema_version": 1,
  "content_hash": "64 lowercase hex"
}
```

它必须与非 null ordered bindings 逐项相等。`speaker_resolution.refs` 稳定按 bindings
顺序、再按 Transcript 中 local speaker 首次出现顺序排列，每项恰好为：

```json
{
  "source_id": "src_...",
  "transcript_version_id": "tr_...",
  "local_speaker_id": "spk_0",
  "resolution": "mapped",
  "person_id": "person_..."
}
```

`resolution` 只允许 `mapped/waived`；`mapped` 要求非 null `person_id`，`waived` 要求
`person_id=null`。`blocking_operation_ids` 只含安全 ID 字符串且不得重复。
两个 subject hash 字段只允许 null 或 64 位 lowercase hex。

`approval_refs` 的每个非 null 值恰好为：

```json
{
  "approval_id": "appr_...",
  "record_schema_version": 1,
  "record_hash": "64 lowercase hex"
}
```

`record_hash` 是完整不可变 evidence 文件的 canonical SHA-256，包含 issued metadata，
但不替代门自己的 subject/dependency hash。`last_receipt_ref` 的非 null 形状恰好为：

```json
{
  "action_id": "act_...",
  "receipt_schema_version": 1,
  "receipt_hash": "64 lowercase hex"
}
```

它只方便当前状态回读；所有历史 receipt 仍按确定性 action ID 路径读取，不把 run 扩展成
事件历史。

## 6. ApprovalRecord schema 1

### 6.1 非秘密与身份边界

ApprovalRecord 是本地审计与门控证据，不是秘密、bearer token、密码、验证码或登录凭据。
知道 approval ID 不授予任何能力；core 仍重新校验 Project、run、stage、subject 和
dependency。它证明“某个 Host channel 通过某个固定 action 提交了明确用户动作”，不证明
密码学意义上的真人身份。Host 没有可信 actor assertion 时，`actor_assurance` 必须是
`unverified_host_user_action`。

### 6.2 不可变 evidence

`workflow/approvals/<approval_id>.json` 创建后不可改写：

```json
{
  "schema_version": 1,
  "approval_id": "appr_...",
  "run_id": "wfr_...",
  "project_id": "proj_...",
  "gate": "scope",
  "subject": {
    "kind": "scope_snapshot",
    "artifact_id": "scope_...",
    "schema_version": 1,
    "content_hash": "64 lowercase hex"
  },
  "dependency_hash": "64 lowercase hex",
  "issued_project_revision": 7,
  "issued_by_action_id": "act_...",
  "source": {
    "channel": "agent_conversation",
    "action": "approve_scope",
    "actor_assurance": "unverified_host_user_action"
  },
  "issued_at": "RFC3339 UTC"
}
```

`gate` 只允许 `scope/brief/outline/draft/roughcut/export`。`source.channel` 只允许
`agent_conversation/review_application`；`source.action` 必须等于签发该 gate 的固定 action。
`issued_project_revision` 是并发 witness，不进入 dependency hash，也不是 freshness 的替代。

gate/action 映射恰好为：

| gate | source.action |
|---|---|
| `scope` | `approve_scope` |
| `brief` | `confirm_brief` |
| `outline` | `approve_outline` |
| `draft` | `approve_draft` |
| `roughcut` | `adopt_roughcut` |
| `export` | `approve_export` |

### 6.3 current/stale 只派生

ApprovalRecord 由对应批准 action 在同一 intent transaction 中创建，并写入
`issued_by_action_id`；action 失败时不发布记录。它不是等待以后“消费”的 capability，
也不另建 `approval-states/` 文件。

每次 `workflow_status` 或 `workflow_action` 都重新读取当前 subject 与固定 dependency
bundle：

- subject/ref 和 dependency hash 均相同：`current`；
- 任一不同：`stale`。

stale 不写回 ApprovalRecord 或 Project；用户再次批准会创建新 approval ID，并替换 run
中当前 gate 的 ref。历史记录仍只读。这样普通 Source metadata、人物、Transcript 校正、
Proxy 或 Draft 机械编辑无需主动改写批准状态；只有它们实际改变固定 dependency 时，下次
status/action 才派生 stale。

### 6.4 canonical JSON 与 SHA-256

所有 content/dependency/input hash 使用同一 `canonical_json_v1`：

1. 输入必须先按对应 closed schema 校验；拒绝重复 key、未知字段、float、NaN、Infinity、
   二进制、无配对 surrogate 和非整数数字；
2. 所有字符串先把 `CRLF`、裸 `CR` 变为 `LF`，再做 Unicode NFC；
3. object key 同样规范化；规范化后重复 key 拒绝；
4. object key 按 Unicode scalar value 升序；array 保持原顺序；
5. JSON 使用 UTF-8、无 BOM、`ensure_ascii=false`、无缩进、key/value 与元素间无空格；
6. 字面量固定为 `true/false/null`，整数使用最短十进制，`-0` 规范为 `0`；
7. hash 输入不含文件末尾换行；
8. `sha256(canonical_bytes).hexdigest()` 以 64 位 lowercase hex 表示。

Workflow 自有时间戳统一为 UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`，固定六位小数和尾部 `Z`。
时间戳参与 ApprovalRecord/receipt 文件的完整性，但不参与 subject/dependency hash。

为避免不同对象同形碰撞，content hash 的输入固定为：

```json
{
  "hash_schema": 1,
  "hash_kind": "subject_content",
  "subject_kind": "<kind>",
  "schema_version": 1,
  "content": {}
}
```

dependency hash 的输入固定为：

```json
{
  "hash_schema": 1,
  "hash_kind": "dependency_bundle",
  "bundle_kind": "<gate>",
  "schema_version": 1,
  "dependencies": {}
}
```

action input hash 的输入固定为：

```json
{
  "hash_schema": 1,
  "hash_kind": "workflow_action_input",
  "run_id": "wfr_...",
  "action_id": "act_...",
  "action": "approve_scope",
  "input": {}
}
```

Workflow 自身的时间戳、approval/run/action ID、absolute path 和当前 wall clock 不进入
subject/dependency hash；projection 明确列出的 Project/domain artifact ID 仍然进入。

领域 artifact 的 subject content projection 固定如下：

| kind | projection |
|---|---|
| `scope_snapshot` | 第 7.1 节列出的全部字段 |
| `brief` | EditBrief schema 1 全部字段 |
| `outline_snapshot` | Outline snapshot schema 1 全部字段 |
| `content_draft` | Content Draft `to_dict()` 全部字段 |
| `proposal` | Proposal schema 1/2 全部字段，但删除 `created_at` |
| `decision` | Decision schema 1/2 全部字段，但删除 `created_at` |
| `export_snapshot` | 第 7.6 节列出的全部字段 |
| `render_manifest` | 已发布 manifest 中 render ID、Decision ref、ordered inputs/clips、output settings、相对输出 ref 与 acceptance；删除工具路径、wall time 和创建时间 |

projection 在 hash 前仍需通过对应现有 artifact schema 与本契约 closed schema 校验。表中没有
列出的字段不得由实现临时加入 hash。

## 7. 六个固定 dependency bundles

本节是完整清单；实现不得自动遍历对象引用、添加隐式依赖或允许调用方删减字段。

### 7.1 素材范围 `scope`

Subject content：

- `project_id`；
- ordered source snapshots：`source_id`、`import_mode`、fingerprint 的
  `size/mtime_ns/sha256_head_tail`、`display_name`、稳定 tags、note；
- 项目输出 `timebase/frame_rate/width/height/audio_sample_rate`；
- ordered ASR authorization：每个 source 的 `transcribe=true|false` 和
  `speaker_diarization=true|false`；
- 用户确认的主线/补充标签只作为 exact tags，不替代 ordered source IDs。

Dependency bundle：

- `project_id`；
- 上述 ordered source IDs 和每项 source snapshot content hash；
- output settings content hash。

不包含 Source locator 或 Project revision。

### 7.2 Brief `brief`

Subject content 是完整 EditBrief schema 1 payload。

Dependency bundle：

- current scope approval 的 subject content hash；
- ordered source IDs；
- output settings content hash。

Brief 可以在 ASR 运行期间确认；Transcript 内容不属于本门依赖。

### 7.3 大纲 `outline`

Subject content 是第 5.3 节 Outline snapshot。

Dependency bundle：

- scope subject content hash；
- Brief artifact ID/schema/content hash；
- exact ordered bindings，每项含 Transcript schema/content hash；
- speaker resolution mode、每个 exact resolution ref，或 waiver subject hash；
- `required_transcripts_ready=true` 与
  `speaker_resolution_ready_or_waived=true` 的 basis hash。

任何 bound Transcript、bindings 顺序、Brief 或相关 speaker resolution 变化都 stale。

### 7.4 初稿 `draft`

Subject content 是当前完整 Content Draft artifact，包括 parent、Brief snapshot、
ordered bindings、context hash、display title、sections、全部 blocks/exact refs 和 narration
状态。

Dependency bundle：

- approved Outline subject content hash；
- Brief artifact ID/schema/content hash；
- exact ordered bindings 与 Transcript content hashes；
- speaker resolution basis hash；
- Content Draft `context_hash`；
- run 当前 workflow anchor ref、用户实际看到并提交的 unconfirmed candidate ref，以及
  从 candidate 逐 parent 到 anchor 的完整有序 ancestry refs。每一代都必须经 Content
  Draft closed schema/content hash/Project ownership及其各自声明的 bindings/context
  校验；普通 ancestry 要求相同 basis，第 8.5 节显式 rebase 只允许 old anchor→first
  rebase child 这一处已验证 basis 边界。不得扫描目录猜 latest，不得越过 anchor 接受
  旧分支。
- `approve_draft` prepare 后，Draft ApprovalRecord 的固定 current basis恰好是
  “批准 subject 仍为该 unconfirmed candidate ref + prepared confirmed child 的
  `parent_draft_id` 精确指向该 subject + Project active Content Draft 与 run anchor 都等于
  该 confirmed child”。confirmed child 的 ID/时间可在 prepare 生成，但其他可继承业务
  内容、parent、确认标记和 narration 状态变换必须按既有 Content Draft 规则唯一确定并在
  marker 前冻结。这是 `draft` bundle 的唯一 stage-specific branch；run anchor 从
  unconfirmed subject 切为该 child 不会令刚签发的 approval stale。

Project revision 不包含在 bundle。与上述字段无关的 revision 变化不 stale。

### 7.5 粗剪采用 `roughcut`

Subject content 是当前完整 Proposal schema 1/2 payload。

Dependency bundle：

- current Draft approval 的 subject content hash；
- active confirmed Content Draft ID/schema/content hash；
- approved Outline content hash；
- Brief content hash；
- exact ordered bindings 与 Transcript content hashes；
- Proposal ID/schema/content hash、base Edit ID 和完整 clip order/ranges；
- Review 当前展示的 basis ID。

播放行为由用户/Review channel 负责，不伪造“已完整观看”的密码学证明；点击采用只能绑定
当前展示 basis。

### 7.6 正式导出 `export`

Subject content 是 fixed export snapshot：

- active Decision ID/schema/content hash；
- ordered clips 的 `clip_id/source_id/source_in_ticks/source_out_ticks`；
- clip count 与 total duration ticks；
- output settings；
- core 生成的 project-relative output target；
- 每个使用 Source 的 fingerprint content hash。

Dependency bundle：

- current Roughcut approval subject content hash；
- active Decision ID/schema/content hash；
- exact ordered bindings 与 Transcript content hashes；
- output settings content hash；
- ordered source fingerprint content hashes；
- export subject content hash。

Proxy、历史 MP4、Host task ID 和聊天记录不属于本门依赖。

## 8. workflow_action 固定表

### 8.0 façade envelope、closed input 与共同类型

第三阶段内部 application façade 的 Python 语义签名固定为：

```text
workflow_start(project, run_id, ordered_source_ids)
workflow_status(project, run_id?)
workflow_action(project, run_id, action_id, action, input)
workflow_cancel(project, run_id, action_id)
```

- `project`、`run_id`、`action_id` 和 `action` 是 envelope，不得在 `input` 中重复；
- `workflow_start.ordered_source_ids` 是有序安全 ID 数组，可以为空；未知、重复、非当前
  Project source 或与 Project 中顺序不一致的 ID 拒绝；
- `workflow_status.run_id` 可省略；省略时只能解析唯一 active run，零个返回
  `workflow_required`，多个返回 `workflow_run_conflict`；
- `workflow_action.input` 必须先解析为本节对应的唯一 schema 1；顶层和所有嵌套对象均为
  closed schema，未知字段、缺字段、重复 key、错误类型和未知 schema 均为
  `workflow_action_invalid`；
- 为消除 omitted/null 两种表达，以下 action input 没有可省略字段。标为 nullable 的字段
  仍必须出现并显式为 `null`；数组可以为空但不能省略；
- Project revision 不是 input 或批准 subject。锁内读取的当前 revision 只作为并发 witness；
- generated artifact/approval/receipt ID、wall clock、绝对路径、FFmpeg/ffprobe 参数、
  Proxy ref 和 Host task ID 不得出现在调用方 input；
- 完整 input 通过 schema 校验、换行/NFC 规范化后，**整个 closed object** 就是第 6.4 节
  `workflow_action_input.input` projection；不得删字段、补默认值或另算“简化 hash”。

共同 `ArtifactRefInputV1` 恰好为：

```json
{
  "artifact_id": "draft_current",
  "schema_version": 1,
  "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`artifact_id` 是安全 ID，`schema_version` 是正整数且必须匹配当前领域对象，
`content_hash` 是 64 位 lowercase SHA-256。`SubjectRefInputV1` 只用于
`return_to_draft`，恰好在上述字段前增加 `kind`，且 `kind` 只允许
`proposal/decision`。所有 ref 都由 façade 读取当前对象、按第 6.4 节 projection 重算后
比较；调用方提供的 hash 不能替代读取和完整 schema 校验。

确认 basis 只允许以下 opaque ref：

```json
{
  "basis_id": "wfb_scope_4b5f41a1c552f3728bd5a95e0cae1e252027a5e3de9c13c62a8230cc6bf88d48"
}
```

`OpaqueConfirmationBasisRefV1` 恰好只有一个必需的 `basis_id`，其值匹配
`^wfb_(scope|brief)_[a-f0-9]{64}$`。它不是 approval、bearer token 或调用方可计算的
content hash。core 从锁内当前 Project/run/artifact 的 closed projection 生成它：

- scope basis 绑定 project/run、current run ordered bindings/authorizations、当前 Project
  selectable Source snapshots、output settings、当前已验证 active Transcript identity
  和 `first_decision_published`；它不含用户本次尚未选择的 target authorization；
- brief basis 绑定 project/run、current scope approval subject、ordered source IDs、
  output settings、当前 active Transcript identities，以及按 binding/首次出现顺序排列的
  eligible unmapped local-speaker refs。

scope basis 的 core-only closed projection 恰好为：

```json
{
  "basis_schema": 1,
  "basis_kind": "scope_confirmation",
  "project_id": "proj_alpha",
  "run_id": "wfr_alpha",
  "current_run_scope": [
    {
      "source_id": "src_a",
      "transcript_version_id": "tr_a_1",
      "transcript_content_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "authorization": null
    }
  ],
  "selectable_project_sources": [
    {
      "source_id": "src_a",
      "source_snapshot_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "active_transcript": {
        "transcript_version_id": "tr_a_1",
        "schema_version": 1,
        "content_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      }
    }
  ],
  "output_settings_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "first_decision_published": false
}
```

两种 projection 中的 `active_transcript` 都为 null 或样例三个字段；数组保持 effective
scope/Project 持久顺序。`current_run_scope.authorization` 为 null，或恰好包含
`transcribe/speaker_diarization` 两个 boolean；两个 Transcript 字段必须同时为 null 或
同时非 null。brief basis 的
core-only closed projection 恰好为：

```json
{
  "basis_schema": 1,
  "basis_kind": "brief_confirmation",
  "project_id": "proj_alpha",
  "run_id": "wfr_alpha",
  "scope_subject_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "ordered_sources": [
    {
      "source_id": "src_a",
      "active_transcript": {
        "transcript_version_id": "tr_a_1",
        "schema_version": 1,
        "content_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      }
    }
  ],
  "output_settings_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "eligible_speaker_waivers": [
    {
      "source_id": "src_a",
      "transcript_version_id": "tr_a_1",
      "local_speaker_id": "spk_0"
    }
  ]
}
```

两种 object 都拒绝 missing/unknown/duplicate key。core 对完整 object 执行
`canonical_json_v1`，scope 返回
`wfb_scope_ + sha256(canonical_bytes).hexdigest()`，brief 返回
`wfb_brief_ + sha256(canonical_bytes).hexdigest()`。这些固定 domain 字段防止跨 kind
碰撞；上面两个样例的 digest 分别为
`4b5f41a1c552f3728bd5a95e0cae1e252027a5e3de9c13c62a8230cc6bf88d48` 与
`15930a3703759d7b7fabbab569d5d9b653cd2171740c6c06aaadf6dd1408f332`。算法只在 core
实现和验证，Host/Agent 不接受计算接口，也不得用自行计算值代替
status 返回值。Host/Agent 只能把 `workflow_status` 返回的整个 opaque ref 原样带回；
不得计算、修改、展示或从聊天重建。普通用户界面展示素材名称、授权选择、剪辑要求和人物
选择，不显示 `basis_id` 或任何技术 hash。action 收到 basis 后，core 先从当前事实重新生成
并恒等比较，再把用户选择加入对应 subject，最后由 core 计算 ApprovalRecord 的
subject/dependency/input hash。Project revision、wall clock 和与该 projection 无关的
metadata 不进入 basis；无关 revision 变化不改变 basis 或制造假 stale。

`SpeakerWaiverInputV1` 是 closed object，恰好为：

```json
{
  "source_id": "src_a",
  "transcript_version_id": "tr_a_1",
  "local_speaker_id": "spk_0"
}
```

三个字段全部必需且都是安全 ID；未知/缺失 nested 字段为
`workflow_action_invalid`。waiver 必须在 brief basis 的 eligible refs 中 exact 命中，
不能只按 `local_speaker_id` 或裸 Transcript ID 匹配。

Action enum **恰好**为：

```text
approve_scope
confirm_brief
submit_outline
approve_outline
submit_draft
approve_draft
return_to_draft
adopt_roughcut
approve_export
```

任何自由文本“继续/出稿”、未知 action 或一个请求内的 action 数组都返回
`workflow_action_invalid`。

### 8.1 `approve_scope`

Input schema `ApproveScopeInputV1`：

```json
{
  "schema_version": 1,
  "confirmation_basis": {
    "basis_id": "wfb_scope_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "source_authorizations": [
    {
      "source_id": "src_a",
      "transcribe": true,
      "speaker_diarization": true
    },
    {
      "source_id": "src_b",
      "transcribe": false,
      "speaker_diarization": false
    }
  ]
}
```

三个字段全部必需。`schema_version` 必须为整数 `1`；
`confirmation_basis` 必须是 status 返回的 exact `OpaqueConfirmationBasisRefV1`；
`source_authorizations` 必须非空、有序且 source ID 唯一，每项恰好含安全
`source_id` 和两个 boolean。`speaker_diarization=true` 要求 `transcribe=true`。
首次批准与 reapproval 的 target 规则恰好采用第 5.2 节；target 必须是 returned basis
selectable sources 的任意非空、有序、去重子集，可以不包含 current run source。空
target、重复、basis 外 source 或首个 Decision 后 binding 变化一律在领域写前拒绝。数组
顺序就是用户明确确认的完整 target ordered scope，不由 Project 目录或 Host 排序。
subject hash 由 current Project source snapshots、input 中这组授权和当前输出设置重建；
聊天、Skill、Host 状态或已有 Transcript 不得产生或补全授权。
每项还必须满足第 5.2 节“有效 active Transcript 或 `transcribe=true`”二选一；basis
失配在领域写前 `workflow_subject_mismatch`，二选一不满足则 `workflow_not_ready`。

- 合法 stage：`scope_review`、`outline_review`、`draft_review`；后两者只允许首个
  Decision 前的显式 reapproval。
- subject：当前 exact scope snapshot。
- 前置：run active；basis current；所有 target source 属于 selectable Project snapshot；
  每项满足 active Transcript/transcribe 二选一。首次批准不允许已有 current scope
  approval；reapproval 要求 current scope approval 且 target scope/order/authorization
  至少一项改变，并要求 `first_decision_published=false`。
- 唯一 transition：`scope_review→scope_review`；从
  `outline_review/draft_review→scope_review`。
- 固定 intent writes：无领域 artifact 写；冻结完整 target ordered bindings/
  authorizations，签发新 scope approval，并按第 5.2 节清理下游 current refs/approval refs；
  Draft rebase anchor 例外保留。
- receipt mutation：`null`。
- stale：subject 或 scope dependency 任一变化，批准 stale。
- 幂等：同 action ID/同 input 返回同 receipt；不同 input 冲突。

### 8.2 `confirm_brief`

Input schema `ConfirmBriefInputV1`：

```json
{
  "schema_version": 1,
  "confirmation_basis": {
    "basis_id": "wfb_brief_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "theme": "校园探访",
  "target_duration_ticks": 7200000,
  "focus": ["学习空间", "人物表达"],
  "allow_reorder": false,
  "speaker_resolution_waivers": []
}
```

七个字段全部必需。`confirmation_basis` 必须是 status 返回的 exact
`OpaqueConfirmationBasisRefV1`，且前缀为 `wfb_brief_`。`theme` 是 NFC/换行规范化后非空字符串；
`target_duration_ticks` 是正整数；`focus` 是有序、非空、每项非空的字符串数组；
`allow_reorder` 是 boolean。`speaker_resolution_waivers` 是可为空的有序 exact ref 数组，
每项是第 8.0 节 `SpeakerWaiverInputV1`，顺序固定为 binding 顺序再按 Transcript 首次出现
顺序；每项必须
存在于当前 active Transcript 且尚无 confirmed Person mapping。列出即表示用户明确放弃
本 run 对该 local speaker 的人物对应，不能由 Agent 自行填入。

调用方不得提供 request/content hash、`brief_id`、`schema_version` 之外的 artifact 字段、
Project revision 或时间。core 用 basis 与完整业务字段计算 input/subject/dependency hash；
prepare 生成唯一 `brief_id` 和时间，并使完整 EditBrief 进入 candidate、ApprovalRecord
和 receipt。

- 合法 stage：`scope_review`、`outline_review`、`draft_review`。
- subject：将创建的完整 EditBrief schema 1。
- 前置：current scope approval；Brief dependency 当前。`scope_review` 中即使已有 current
  Brief approval，`confirm_brief` 仍合法：当后续 ASR 暴露新的 unmapped speaker 时，用户
  可保持 theme/duration/focus/reorder 不变，只用新 action ID 和当前 brief basis 增加
  exact waiver。`draft_review` 中 input 的 theme、target duration、focus、allow_reorder
  至少一项必须不同于 current Brief；waiver-only 或逐字段相同的 input 在领域写前以
  `workflow_action_invalid` 拒绝，core 不从聊天判断“用户是否想改要求”。
- 唯一 transition：在 `scope_review` 无；在 `outline_review/draft_review` 修改要求时
  回到 `scope_review`。旧 Brief/Outline/Draft artifact 保持不可变历史；run 设置 prepared
  Brief ref，清 current Outline/Proposal/Decision/Render refs及 Brief 之后的 approval
  refs，但保留旧 Draft workflow anchor 作为第 8.5 节 requirements-rebase parent。
- 固定 intent writes：调用现有 Brief create/activate 语义，创建一个 immutable Brief
  并更新 Project revision；若从 `outline_review/draft_review` 返回，同时按上项更新 run。
- receipt mutation：新 Brief ID/schema/content hash。
- stale：scope 或 output settings 变化、active Brief 被替换时 stale。
- 幂等：由 receipt 回读原 Brief；不得重复创建第二个 Brief。

revision/幂等规则固定为：每个**新** action ID 的成功 `confirm_brief` 都创建一个新的
immutable Brief、使 Project revision 恰好 `+1` 并签发新的 Brief approval；即使四个剪辑
业务字段逐项不变而只新增 waiver 也如此。同 action ID/同 closed input 只回读原 receipt，
revision `+0`；同 action ID/任一 waiver 或业务字段不同为 `workflow_action_conflict`。

### 8.3 `submit_outline`

Input schema `SubmitOutlineInputV1` **就是**完整 Outline snapshot schema 1：

```json
{
  "schema_version": 1,
  "title": "从学习空间认识校园",
  "opening": "主持人原话直接进入主题",
  "sections": [
    {
      "section_id": "section_1",
      "title": "开场",
      "summary": "主持人说明探访目标",
      "target_duration_ticks": 1200000
    },
    {
      "section_id": "section_2",
      "title": "图书馆",
      "summary": "介绍学习空间",
      "target_duration_ticks": 1200000
    },
    {
      "section_id": "section_3",
      "title": "实验室",
      "summary": "介绍设备与课程",
      "target_duration_ticks": 1200000
    },
    {
      "section_id": "section_4",
      "title": "结尾",
      "summary": "主持人总结体验",
      "target_duration_ticks": 1200000
    }
  ],
  "ending": "主持人原话总结",
  "required_content_coverage": [
    {
      "requirement": "必须保留图书馆",
      "covered": true,
      "evidence_refs": [
        {
          "source_id": "src_a",
          "transcript_version_id": "tr_a_1",
          "segment_id": "seg_2",
          "start_ticks": 120000,
          "end_ticks": 240000
        }
      ]
    }
  ],
  "narration_status": "none"
}
```

字段、递归 closed shape 和约束恰好采用第 5.3 节：非空、有序的 `sections` 数组、唯一安全
`section_id`、正整数 duration、真实 exact evidence refs，以及固定
`none/pending/to_write/recorded` narration enum。所有顶层字段必需。不存在
`instruction`、`continue`、`approved` 或自由文本“按此出稿”字段；大纲提交只保存这个
完整结构化 snapshot，不批准它。

- 合法 stage：`scope_review`、`outline_review`。
- subject：完整 Outline snapshot schema 1。
- 前置：scope/Brief approval current；required Transcripts ready；speaker resolution
  ready or waived；blocking operation IDs 为空；outline dependency 当前。
- 唯一 transition：首次提交为 `scope_review -> outline_review`；在 `outline_review`
  重新提交另一版大纲时无 transition，并替换当前尚未批准的 Outline ref。
- 固定 intent writes：无领域写；snapshot/hash 只写入 run。
- receipt mutation：`null`，output ref 为 outline ref。
- stale：任一 outline dependency 变化。
- 幂等：重复只回读同 snapshot/receipt。

### 8.4 `approve_outline`

Input schema `ApproveOutlineInputV1`：

```json
{
  "schema_version": 1,
  "outline_ref": {
    "artifact_id": "outline_cccccccccccccccc",
    "schema_version": 1,
    "content_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
}
```

两个字段全部必需；`outline_ref` 恰好为 `ArtifactRefInputV1`，artifact ID 必须等于
`outline_<content_hash 前 16 位>`。façade 必须读取 run 当前完整 snapshot、重算 hash，
并逐字段比较当前 Review/Agent 展示 ref；仅提交 hash 字符串、Project revision 或另一版
Outline 都是 `workflow_subject_mismatch`。ref shape/type 非法才是
`workflow_action_invalid`；安全但前缀/hash/current identity 不同属于 subject mismatch。

- 合法 stage：`outline_review`。
- subject：run 当前 exact Outline ref/snapshot。
- 前置：outline dependency 当前；请求 subject 必须逐 hash 等于当前展示大纲。
- 唯一 transition：`outline_review -> draft_review`。
- 固定 intent writes：无领域写；只写 approval、run 和 receipt。
- receipt mutation：`null`。
- stale：Outline 或其固定依赖变化。
- 幂等：同 ID 回读；stage 已推进后，另一个 action ID 不可再次批准同一大纲。

### 8.5 `submit_draft`

Input schema `SubmitDraftInputV1`：

```json
{
  "schema_version": 1,
  "parent_draft_ref": null,
  "display_title": "从学习空间认识校园",
  "source_bindings": [
    {
      "source_id": "src_a",
      "transcript_version_id": "tr_a_1"
    }
  ],
  "brief_ref": {
    "artifact_id": "brief_current",
    "schema_version": 1,
    "content_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "context_hash": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "blocks": [
    {
      "block_id": "heading_block_1",
      "kind": "section_title",
      "title": "开场"
    },
    {
      "block_id": "block_1",
      "kind": "source_excerpt",
      "refs": [
        {
          "source_id": "src_a",
          "transcript_version_id": "tr_a_1",
          "segment_id": "seg_1",
          "start_ticks": 0,
          "end_ticks": 120000
        }
      ],
      "canonical_text": "欢迎来到校园。"
    },
    {
      "block_id": "heading_block_2",
      "kind": "section_title",
      "title": "图书馆"
    },
    {
      "block_id": "block_2",
      "kind": "narration",
      "text": "接下来看看图书馆。",
      "status": "draft",
      "recorded_refs": []
    }
  ],
  "scoped_mutable_block_ids": []
}
```

八个字段全部必需。`parent_draft_ref` 是 nullable `ArtifactRefInputV1`；
`display_title` 是 nullable、规范化后 1–80 字符字符串；`source_bindings` 非空、有序、
source ID 唯一且逐项等于 run ordered bindings；`brief_ref` 是当前 Brief exact ref；
`context_hash` 是 64 位 lowercase SHA-256；`blocks` 是非空完整 candidate。

`source_excerpt` block 恰好含
`block_id/kind/refs/canonical_text`；`narration` block 恰好含
`block_id/kind/text/status/recorded_refs`；独立 `section_title` block 恰好含
`block_id/kind/title`。source/narration 不得内嵌标题。所有 ref 恰好含
`source_id/transcript_version_id/segment_id/start_ticks/end_ticks`，ticks 是非负整数半开
区间。block ID 全局唯一；来源 canonical text、连续 ref、narration 状态和 recorded refs
继续由现有 Content Draft 规则验证。`scoped_mutable_block_ids` 是安全、唯一、有序 ID
数组；空数组表示完整新稿，非空时所有未列 parent blocks 必须逐字段保持。

调用方不得提供 `content_draft_id`、`base_project_revision`、`confirmed_by_user` 或时间；
prepare 生成 candidate ID，Project revision 只在锁内验证。最终 subject 是 prepare 后的
完整 unconfirmed Content Draft artifact。

`parent_draft_ref=null` 只在 run 尚无 Draft anchor 的第一次提交合法，并把新 candidate
设为 anchor。anchor 已存在时 `parent_draft_ref` 必须是 anchor 本身或逐 parent 验证属于
该 anchor 的不可变后代；新 candidate 是该 exact parent 的 child，但不因目录顺序或创建
时间自动替换 anchor。Review 的页面删除、移动、插入、分支和 undo/redo 产生的 immutable
child 同样只能通过显式 exact ref 选择；两个 sibling 都不会因“较新”自动胜出。

首个 Decision 前发生 scope reapproval 或 `draft_review` 修改 Brief 后，保留的旧 anchor
允许一次手写 **Draft rebase**：

- `parent_draft_ref` 必须逐 hash 等于该 exact 旧 anchor，不能选目录中更新的 child；
- `source_bindings/brief_ref/context_hash` 必须完整等于新 run/current approvals；
- 提交的是完整 immutable child。binding rebase 必须列出完整新 bindings；requirements
  rebase 必须以重新批准的 Outline 为第一次新结构基础；
- `scoped_mutable_block_ids` 非空时，只允许这些旧 block 被删除、移动或修改内容；章节
  标题修改必须点名对应的独立 heading block ID。所有未列且其 refs 在新 bindings 中仍
  有效的旧 blocks，其 ID、kind、refs、相对顺序、文字/narration 状态和 section metadata
  必须逐字段不变；
- 新 block 可以使用新 binding 并使用新 ID。旧 block 引用已移除/替换 binding 时必须列入
  mutable set；否则只能走下一项明确全稿重做。不能自动重绑裸 `segment_id`；
- parent 非 null 且 `scoped_mutable_block_ids=[]` 时，若全部旧 blocks 仍逐字段同序保留、
  只增加新 block，则是 additive binding rebase；否则表示用户明确要求全稿结构重做，
  Host 只有取得该明确指令才可提交完整 rewrite child。core 以 parent/current 完整 diff
  唯一判别两种 closed case，不读取聊天内容。无论 scoped/additive rebase 还是 full
  rewrite，都必须经过后续 `approve_draft` 最终结构确认门。
- `prepare_content_draft` 在 marker 前同时冻结 exact old anchor 和以 prepared
  unconfirmed rebase child 为 anchor 的 `run_after`。publish 顺序仍是 candidate→run→
  receipt；它们构成一个逻辑提交边界。receipt 前任一点退出由第二阶段恢复器还原 exact
  old anchor并只删除本 action 拥有的新 child；receipt 已发布则 committed reconciler
  回读同一 receipt 和新的 unconfirmed anchor，不再次生成 child。
- rebase receipt 成功后，该特殊 old-basis boundary 已消费；之后同 basis 的普通
  `submit_draft` child 只作为显式候选返回，不自动替换 WorkflowRun anchor。old anchor 与
  它的完整 parent chain 仍为不可变历史，core 不扫描目录寻找更新版本。

普通后续 scoped revision 同样允许用户点名独立 heading block 改 `title`；`display_title`
可随该次明确结构请求改变。未点名 blocks 的 section metadata 与其他字段仍逐字段不变。首次
Draft 仍以 approved Outline 为基础，但不会永久锁死所有 descendants 的标题。

- 合法 stage：`draft_review`。
- subject：一个完整 unconfirmed Content Draft candidate；可声明 anchor 内的 exact
  parent 和 scoped
  mutable block IDs，但最终仍提交完整 candidate。
- 前置：Outline approval current；普通 revision 的 Draft dependency 当前；上述一次性
  rebase 允许旧 anchor 的 Brief/binding/context dependency stale，但必须逐项等于
  reapproval 前冻结事实并满足新的完整 rebase 校验。所有 exact refs 通过现有 core
  校验；未点名 parent blocks 保持既有规则。
- 唯一 transition：无。
- 固定 intent writes：创建一个 immutable unconfirmed Content Draft candidate；仅首次
  Draft 或第一个合法 rebase child 同时把 run anchor 原子切为该 candidate，普通同 basis
  后续 child 不切换。
- receipt mutation：新 Content Draft ID/schema/content hash。
- stale：anchor、parent ancestry、Outline、Brief、binding、Transcript、context 或
  speaker basis 变化。
- 幂等：同 ID 不创建第二个 child。

Review 内删除、移动、加入、narration edit 和 Draft undo/redo 不是该 action；它们继续使用
现有 mechanical editor child/session 规则。

### 8.6 `approve_draft`

Input schema `ApproveDraftInputV1`：

```json
{
  "schema_version": 1,
  "content_draft_ref": {
    "artifact_id": "draft_current",
    "schema_version": 1,
    "content_hash": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
  }
}
```

两个字段全部必需；ref 必须是 run 当前 workflow anchor 本身或其经验证的不可变后代，
必须为 unconfirmed，并与 Review 当前展示 candidate 相同；不要求它逐项等于 anchor。
调用方不得提供确认后 child ID、Proposal ID、Project revision 或第二个 action。prepare
生成 confirmed child 和唯一 Proposal，二者在同一 marker 中固定；
Draft ApprovalRecord subject 仍是用户看到的 unconfirmed ref，其 dependency hash 使用
第 7.4 节 prepared post-action pair，因此成功后不会因确定性的 confirmed child 激活而
立即假 stale。

- 合法 stage：`draft_review`。
- subject：用户实际看到且属于 run 当前 anchor 的 exact unconfirmed Content Draft
  ID/schema/content hash。
- 前置：Outline approval current；Draft dependency 当前；candidate 的完整 parent chain
  属于 anchor，且 exact candidate 与 Review 展示 ID/hash 相同；所有需要入片的 narration
  已绑定 recorded refs。
- 唯一 transition：`draft_review -> roughcut_review`。
- 固定 intent writes：现有 Content Draft confirm 语义创建 confirmed child、激活它并增加
  一次 Project revision；随后只从该 confirmed Draft 固定编译一个 current Proposal，并把
  exact ref 写回 run。两项属于同一个“确认初稿并生成待审粗剪”事务，不得夹带粗剪采用。
- receipt mutation：以 confirmed Content Draft 为主摘要，Proposal ref 进入 output refs。
- stale：candidate、anchor/parent ancestry、Outline、Brief、binding、Transcript 或
  context 变化。
- 幂等：响应丢失后回读同 confirmed child，不再次增加 revision。

### 8.7 `return_to_draft`

Input schema `ReturnToDraftInputV1`：

```json
{
  "schema_version": 1,
  "current_subject_ref": {
    "kind": "proposal",
    "artifact_id": "proposal_current",
    "schema_version": 2,
    "content_hash": "1111111111111111111111111111111111111111111111111111111111111111"
  },
  "confirmed_content_draft_ref": {
    "artifact_id": "draft_confirmed",
    "schema_version": 1,
    "content_hash": "2222222222222222222222222222222222222222222222222222222222222222"
  }
}
```

三个字段全部必需。`current_subject_ref` 恰好为 `SubjectRefInputV1`：在
`roughcut_review` 必须是 run 当前 `proposal`，在 `export_review` 必须是 run 当前
`decision`；`confirmed_content_draft_ref` 必须是该 Proposal/Decision ancestry 中 run
当前 confirmed Draft。两者任一 hash、schema、binding 或 ancestry 不同都拒绝。

- 合法 stage：`roughcut_review`、`export_review`。
- subject：当前展示的 Proposal 或 active Decision exact ref，以及要重新打开的 confirmed
  Draft ref。
- 前置：subject 属于当前 run 且 ancestry/bindings 匹配。
- 唯一 transition：当前合法 stage `-> draft_review`。
- 固定 intent writes：无领域写；既有 artifact 不改写，只更新 run。
- receipt mutation：`null`。
- 结果：run 的 Proposal/Decision/Render current refs 置 null；confirmed Draft workflow
  anchor、Outline/Brief refs 保留。历史 roughcut/export ApprovalRecord 不改写，其状态因
  current subject 缺失而派生为 stale；后续新 Draft 必须显式以该 anchor 或其后代为 parent。
- stale：页面 basis 或 current run ref 已变化时拒绝。
- 幂等：同 ID 回读同 draft_review receipt。

### 8.8 `adopt_roughcut`

Input schema `AdoptRoughcutInputV1`：

```json
{
  "schema_version": 1,
  "proposal_ref": {
    "artifact_id": "proposal_current",
    "schema_version": 2,
    "content_hash": "3333333333333333333333333333333333333333333333333333333333333333"
  }
}
```

两个字段全部必需；ref 必须逐项等于 run current Proposal 和 Review 当前 basis，并按
Proposal schema 1/2 重算 content hash。调用方不能提交 clip、裸 `segment_id`、Decision
ID 或“采用并导出”。prepare 按 Proposal schema 生成唯一 Decision。

- 合法 stage：`roughcut_review`。
- subject：当前完整 Proposal ID/schema/content hash。
- 前置：Draft approval current；Proposal 由 run 当前 confirmed Draft 编译；
  roughcut dependency 当前；若审阅确实要求 Proxy，则其 blocking operation 已成功。
- 唯一 transition：`roughcut_review -> export_review`。
- 固定 intent writes：调用 schema-matched Proposal confirm，创建一个 immutable
  Decision、激活它并增加一次 Project revision。
- receipt mutation：Decision ID/schema/content hash。
- stale：Proposal、Draft、Outline、Brief、bindings、Transcript 或 Review basis 变化。
- 幂等：同 ID 回读同 Decision；stage 已推进后，另一个 action ID 不可再次采用同一
  Proposal。

### 8.9 `approve_export`

Input schema `ApproveExportInputV1`：

```json
{
  "schema_version": 1,
  "export_ref": {
    "artifact_id": "export_4444444444444444",
    "schema_version": 1,
    "content_hash": "4444444444444444444444444444444444444444444444444444444444444444"
  }
}
```

两个字段全部必需；artifact ID 必须等于 `export_<content_hash 前 16 位>`。façade 在
展示前从 active Decision、ordered clips、output settings、受控 project-relative target
和 ordered Source fingerprints 构造第 7.6 节完整 export snapshot；批准时读取当前事实并
重算，ref 必须逐项相同。调用方不得提供或覆盖 snapshot 内容、绝对/相对输出路径、
FFmpeg/ffprobe 参数、编码 flag、Proxy ref、Render ID 或 Project revision。Render ID、
正式 target 和工具解析只由 `PreparedExport` 从已冻结 Project/export snapshot 派生。
ref shape/type 非法是 `workflow_action_invalid`；安全但 prefix/hash/current export
identity 不同是 `workflow_subject_mismatch`。

- 合法 stage：`export_review`。
- subject：完整 export snapshot。
- 前置：Roughcut approval current；active Decision 与 subject 相同；export dependency
  当前；原素材 fingerprint 当前；没有阻塞中的同输入 render operation。
- 唯一 action transition：`export_review -> exporting`。
- 固定 intent writes：为 exact Decision 创建/冻结 Render Plan，执行并验证一次正式
  Render，原子发布 MP4/manifest；不得读取 Proxy，也不得顺带采用另一个粗剪。
- receipt mutation：render ID、project-relative MP4/manifest refs 和 acceptance summary。
- stale：Decision、bindings、Transcript、output settings、source fingerprint 或 output
  target 变化。
- 幂等：同 ID/同输入只回读同 render/operation/receipt；不同输入冲突。tracked
  failed/interrupted 不可用同 ID 重进 Render，只能按第 10.4 节用新 action/operation ID
  显式重跑。

同步实现必须把失败视为 action 未提交并恢复到 `export_review`；媒体长任务按 Project
Media OperationRecord 契约保存独立终态，同 ID 只回读，显式重跑遵守第 10.4 节。任何
情况下都不得因响应丢失再次发布 MP4。

### 8.10 input hash、未知字段与 cancel

九个 action 的 `input_hash` 都是第 6.4 节 envelope 加本节完整 closed input 的
`canonical_json_v1` SHA-256。object key 的输入顺序不影响 hash；数组顺序有语义，不能
排序。相同 run/action ID/action、相同规范化 input 必须得到相同 hash；任一业务字段、
nullable 值、ref hash、authorization 顺序、block 或 exact ticks 改变都得到不同 hash。
Project current revision、generated ID、prepare 时间和 staging 路径不加入 input，也不影响
同输入重试。

若同 `action_id` 已有 receipt/marker：

- hash 相同：进入既有恢复/回读路径，不再次 prepare；
- hash 不同：`workflow_action_conflict`；
- input schema 本身非法：按错误优先级先返回 `workflow_action_invalid`，不得用调用方提供
  的预计算 hash 绕过解析。

`workflow_cancel` 没有 `input` 参数；其 canonical input 固定视为 `{}`：

```json
{
  "hash_schema": 1,
  "hash_kind": "workflow_action_input",
  "run_id": "wfr_alpha",
  "action_id": "act_cancel",
  "action": "workflow_cancel",
  "input": {}
}
```

同 run/action ID 的 cancel 重试因此得到同一 hash 和 receipt；不同 action ID 是新的
envelope，但 canceled run 不接受第二次取消。调用方不能向 cancel 增加 reason、delete、
operation 或媒体字段。

## 9. receipt 与幂等

成功 ActionReceipt schema 1：

```json
{
  "schema_version": 1,
  "action_id": "act_...",
  "input_hash": "64 lowercase hex",
  "run_id": "wfr_...",
  "project_id": "proj_...",
  "action": "approve_draft",
  "before": {
    "stage": "draft_review",
    "lifecycle": "active",
    "project_revision": 12
  },
  "after": {
    "stage": "roughcut_review",
    "lifecycle": "active",
    "project_revision": 13
  },
  "approval_ids": ["appr_..."],
  "mutation": {
    "kind": "content_draft",
    "artifact_id": "draft_...",
    "schema_version": 1,
    "content_hash": "64 lowercase hex",
    "changed": true
  },
  "output_refs": [
    {
      "kind": "proposal",
      "artifact_id": "proposal_...",
      "schema_version": 1,
      "content_hash": "64 lowercase hex",
      "project_relative_path": null
    }
  ],
  "created_at": "RFC3339 UTC"
}
```

规则：

- `action_id` 由调用方生成，run 内全局唯一；
- receipt 路径由 action ID 确定；成功后不可改写；
- `mutation` 只允许 `null` 或样例中的 closed object；`kind` 只允许当前 action 表指定的
  `brief/content_draft/decision/render`；
- `output_refs` 的每项恰好为
  `{kind, artifact_id, schema_version, content_hash, project_relative_path}`；
  `project_relative_path` 只允许正式 Render 的 MP4/manifest，其他 output ref 必须为
  `null`，且所有路径重新执行 containment；
- `submit_outline` 恰好返回一个 pathless `outline` ref；`approve_draft` 恰好返回一个
  pathless `proposal` ref；`approve_export` 恰好返回两个 kind 分别为 `mp4` 和
  `manifest` 且带相对路径的 refs；其他 action 的 `output_refs` 必须为空；
- `mutation.kind` 必须逐 action 等于第 8 节的 `null/brief/content_draft/decision/render`，
  不能用合法但属于另一 action 的 mutation 通过 schema；
- `workflow_cancel` 只属于 receipt/transaction action 闭集，不属于九个
  `workflow_action` 业务枚举；其 before 必须为 active，after 保持相同 stage 并变为
  canceled，Project revision 不变，approval/mutation/output 全部为空；
- 已存在 receipt 且 input hash 相同：返回逐字段相同的 receipt 和重新派生的
  readiness/actions，不再调用领域 service；
- 已存在 receipt 或 transaction marker 且 input hash 不同：
  `workflow_action_conflict`；
- 领域 no-op 仍可有 `changed=false` receipt，但不得用 no-op 穿越第二道门；
- 稳定拒绝不创建成功 receipt。

## 10. 项目锁、提交顺序与失败原子性

### 10.1 锁

所有会写 `project.json` 的既有 `ProjectStore` 操作，以及 protected write、workflow
action、run/approval/receipt 操作，统一取得同一 Project write lock：

1. 进程内按 resolved Project root 的 mutex；
2. Project root 下 `.roughcut-project.lock` 的跨进程独占 advisory lock；
3. 再读取 Project/run/approval/receipt。

macOS/Linux 使用 `flock`，Windows 使用一字节 `msvcrt.locking`；行为必须由同一 adapter
封装，并允许同一进程内由 workflow façade 持锁调用现有 `ProjectStore` 而不重复争锁。
lock 文件也执行 containment/symlink/regular-file 检查。不得先取得领域子锁再等待
Project write lock；首版只有这一个**业务状态写锁**。第 10.4 节 transient export claim
不是领域/业务状态锁，固定在 Project write lock 之前取得，普通 Project/workflow
读写永不等待它，因而不形成反向锁序。Project write lock 只解决写入串行化，不使普通写
操作理解 run、approval 或 workflow transaction。

纯读取不创建或修改 ApprovalRecord；若读取发现 transaction marker，必须取得写锁先恢复，
再返回。

### 10.2 固定 action 提交协议

每个 action 使用手写 participant 清单，不建设可配置事务框架：

1. 在锁内验证 containment、active run、receipt/input hash、stage、subject、readiness、
   approval 和 dependency；
2. 读取并冻结 before Project/run/approval hash；
3. 调用第 10.3 节该 action 唯一的 prepare helper，**只在内存中**生成所有 ID、时间、
   immutable candidate、Project after、WorkflowRun after、ApprovalRecord 和 receipt；
   prepare 只能读取、验证和构造对象，禁止写 artifact、Project、run、approval、receipt、
   marker 或 staging 文件；
4. 从 prepare 的精确结果构造并 fsync `transactions/<action_id>.json`；除
   action/input hash、before/after
   文件 hash、candidate refs 和 fixed commit step 外，还保存 exact Project/Run before
   JSON 与本 action 新建的 approval IDs。before payload 必须逐 hash 等于记录值，不含
   媒体、Transcript 正文、代理或任意日志；
5. 只发布 prepare 返回的精确对象，固定顺序为：该 action 的 immutable domain
   candidates → 不同于 before 的 Project after → ApprovalRecord → WorkflowRun after →
   receipt。每一步原子写并 fsync 文件/目录；不得重新生成 ID/时间、重新解析输入或重新
   计算另一份 candidate；
6. receipt 最后发布，是唯一逻辑成功点；
7. 用第二阶段 committed reconciler 删除 transaction marker，并回读同一 receipt。

对无领域写的 action，domain candidate 和 Project publish 是空步骤，但 prepare 仍必须
构造 exact run/approval/receipt。锁从步骤 1 持有到 marker 清理；唯一例外是第 10.4 节
Render staging。旧 artifact 永不改写。

prepare 可以生成随机 ID 和 wall clock，但它们必须在 marker 发布前全部确定，进入
candidate payload/content hash、Project/Run after hash、approval 和 receipt。既有 public
application service 后续可以改为复用相同的 pure prepare helper 和单对象 publish helper；
不得让 façade 调用一个会自行生成另一组 ID/时间并立即写盘的旧 service。该重构属于第三
阶段生产实现，本轮不实施。

#### 10.2.1 Host-compatible crash-safe candidate publication 私有接缝

`publish_workflow_candidate` 当前对 dict candidate 执行 temp → final hardlink 后 unlink
temp，对 `StagedWorkflowFile` 执行 staged → temp → final hardlink 后 unlink staged/temp。
已支持的 WorkBuddy workspace 可复现：可见源路径消失后 Host 仍保留隐藏硬链接，final
`nlink` 分别稳定为 2/3，`WorkflowStore` 的 single-link 校验随后正确拒绝正式发布。
Roughcut core 的 hardlink/unlink 发布原语是失败责任主体，Host 行为只是触发条件；系统
临时目录 `nlink=1` 的探针不推翻 workspace 证据。TransactionMarker rollback、旧 staging
隔离或新 operation ID 也不改变该发布原语。

所有第 10.2 节 marker-owned workflow candidate，包括内存 dict payload 与
`StagedWorkflowFile`，必须只经过一个 workflow-private publication seam：

1. dict candidate 按既有持久 JSON 文件序列化规则完整写入 final 同一 filesystem、
   core-owned temp；本 P1 不迁移或改变既有 candidate JSON 文件的持久字节格式，
   content/ref/input hash 继续使用既有 `canonical_json_v1` / `canonical_sha256_v1`。变化只限
   publication primitive：hardlink/unlink → atomic no-replace rename/move；
   `StagedWorkflowFile` 必须已经在同一 filesystem 的 core-owned staging 中完整写完。
   发布前验证内容与身份并 fsync 完整候选；
2. 使用支持平台的 OS 原生 **atomic no-replace rename/move** 将该完整 temp/staged source
   发布为 final。destination 已存在必须 fail closed，绝不覆盖；不得先检查再执行可覆盖
   操作；
3. 成功 rename 消费 source 目录项；实现不得再依赖 unlink temp/staged 后隐藏硬链接消失或
   `nlink` 收敛。发布成功的 final 必须是普通、单链接文件，并通过原有内容/ref 校验；
4. 按 crash durability 要求 fsync 必要的 source 与 destination 父目录；二者相同时只按
   平台正确语义处理同一目录。rename 前、rename 后、目录 fsync 前后任一点退出，都必须由
   第 10.2 节既有 TransactionMarker exact-before/receipt 语义恢复；
5. macOS 与 Windows 分别使用各自已证明可靠的原生 primitive。任一支持平台若不能同时
   保证 atomic 与 no-overwrite，立即停止实现并复评，不得退化为
   check-then-`os.replace`。

该接缝不修改 TransactionMarker、artifact、Project 或 WorkflowRun schema，也不修改公开
tool 参数、workflow action/input。禁止使用 `os.replace` 或任何允许覆盖 destination 的
操作；禁止依据 `CODEBUDDY_SAFE_DELETE_BIN_DIR`、Host 名称或环境变量改变业务语义；禁止
放宽 single-link 校验、扫描或删除隐藏硬链接、copy-then-delete 大型 staged MP4；禁止把
接缝扩大为其他 store 的通用文件 API、filesystem framework、daemon、queue、retry 或恢复
服务。

实现验收必须覆盖 dict candidate 和 `StagedWorkflowFile`：destination 预存在时零覆盖；
逐故障点断言 staged/temp/final 的精确存在状态、inode、`nlink` 与内容；覆盖 rename 前、
rename 后、目录 fsync 前后的崩溃及既有 marker recovery；覆盖 macOS/Windows 分支和既有
workflow transaction/recovery 回归。媒体 staged-file 测试只使用小型字节 fixture，不读取
真实媒体。独立代码评审通过后，由 WorkBuddy 在 workspace 内运行 dict 与 5-byte
`StagedWorkflowFile` 原生探针，并跨独立 shell 验证 final `nlink=1`、temp/staged source
均不存在。该探针不得运行 Render 或触碰真实 W5 Project。探针通过前不得隔离旧事故
staging、公开状态重入、完整质量门、W5 UX 或正式 Render。生产实现、独立代码评审与该
WorkBuddy 探针现均已通过，本 P1 据此关闭；该证据不替代 Render、W5 或用户 UX。W5、
M2.6 与 M3 均未完成，前一个原生 MCP 长调用/媒体收敛 P1 保持已通过。

### 10.3 九个业务 action 与 cancel 的手写 Prepared 结果

本节名称和字段是内部 application 接缝，不是持久 schema，也不允许由调用方构造。每个
结果是 frozen、action-specific value；不存在共同的动态 participant base class、注册表、
插件事务、DSL 或依赖图。

| prepare helper | frozen result | 内存中必须已经确定的字段 |
|---|---|---|
| `prepare_scope_approval` | `PreparedScopeApproval` | current/selectable scope basis、完整 target bindings/authorizations、`first_decision_published` witness、`scope_subject`、`scope_dependency_hash`、exact `ApprovalRecord(scope)`、下游 refs 清理/旧 Draft rebase anchor 保留的 `run_after`、`receipt`；`project_after` 与 before 同一对象 |
| `prepare_brief` | `PreparedBrief` | generated `EditBrief`、Brief candidate ref/payload、`project_after`、speaker waiver basis、exact `ApprovalRecord(brief)`、outline/draft review 返回时清下游 refs并保留旧 Draft rebase anchor 的 `run_after`、`receipt` |
| `prepare_outline` | `PreparedOutline` | canonical Outline snapshot/ref、`run_after`、`receipt`；无 filesystem candidate/approval，Project 不变 |
| `prepare_outline_approval` | `PreparedOutlineApproval` | current Outline subject/dependency、exact `ApprovalRecord(outline)`、`run_after`、`receipt`；Project 不变 |
| `prepare_content_draft` | `PreparedContentDraft` | validated exact old workflow anchor/explicit parent ancestry、可选的单次 binding/requirements rebase boundary、scoped/full structure intent、generated unconfirmed `ContentDraft`、Draft candidate ref/payload、`run_after`（首次 Draft 或首次合法 rebase 时 anchor 切为 prepared unconfirmed child；普通同 basis child 保持 anchor）、`receipt`；Project 不变 |
| `prepare_confirmed_draft_and_proposal` | `PreparedConfirmedDraftAndProposal` | 用户展示的 exact unconfirmed anchor descendant、确定性 parent→generated confirmed child 映射、generated confirmed Draft 与 Proposal 两个 ref/payload、`project_after`、exact `ApprovalRecord(draft)`、anchor 切为 confirmed child 的 `run_after`、以 confirmed Draft 为 mutation 且 Proposal 为 output 的 `receipt` |
| `prepare_return_to_draft` | `PreparedReturnToDraft` | verified Proposal/Decision 与 confirmed Draft ancestry、清除 Proposal/Decision/Render current refs 的 `run_after`、`receipt`；无 candidate/approval，Project 不变 |
| `prepare_decision` | `PreparedDecision` | generated schema-matched Decision ref/payload、`project_after`、exact `ApprovalRecord(roughcut)`、`run_after`、`receipt` |
| `prepare_export_publish` | `PreparedExport` | 已验证 staging 中的 Render Plan、MP4、manifest 三个 exact ref/payload/hash、`project_after`（与 before 相同）、exact `ApprovalRecord(export)`、`run_after=exporting/completed`、render mutation、MP4/manifest outputs 和 `receipt` |
| `prepare_workflow_cancel` | `PreparedWorkflowCancel` | `project_after` 与 before 同一对象、相同 stage 且 lifecycle 为 canceled 的 `run_after`、空 approval/mutation/output 的 `workflow_cancel` receipt |

每个 Prepared 还必须保存 exact `project_before`、`run_before`、`input_hash`、按上表固定顺序
的 candidate refs、approval IDs 和 controlled final relative paths；这些字段只能由 helper
从当前 Project root 与安全 ID 构造。不得保存调用方路径。`project_after` 即使等于 before
也必须是显式字段，便于 marker hash 和恢复器对 Project/Run 成对验证。

固定 participant/publish/rollback 表：

| action | current reads and validation | candidate publish order | Project after | run transition | approval | receipt/output | receipt 前 crash 的 recovery/owned rollback |
|---|---|---|---|---|---|---|---|
| `approve_scope` | core 重建 current/selectable opaque scope basis、完整 target authorizations、active Transcript 或 transcribe、首个 Decision witness | 无 | 字节等于 before | 首次/reapproval 均到 `scope_review`；冻结 target bindings/authorizations，清下游 refs，保留 exact old Draft rebase anchor | `scope` | mutation null、无 output | 恢复 exact run before；删除本 action scope approval；历史 artifact 不删 |
| `confirm_brief` | core 重建 opaque brief basis、current scope approval、scope bundle、当前 Project/output settings、closed waiver refs | `briefs/<brief_id>.json` | revision `+1`，active Brief 指向 prepared ID | `scope_review→scope_review` 或 `outline_review/draft_review→scope_review`，设置 Brief ref、清下游 current refs并保留 old Draft rebase anchor | `brief` | Brief mutation、无 output | 恢复 Project/run before；删除 prepared `briefs/<brief_id>.json` 和 Brief approval；历史 artifact 不删 |
| `submit_outline` | current scope/Brief approvals、required Transcripts、speaker basis、blocking IDs、完整 Outline refs | 无；Outline snapshot 只在 run | 字节等于 before | `scope_review→outline_review` 或 `outline_review→outline_review`，设置 prepared Outline ref/snapshot | 无 | mutation null、一个 pathless Outline output | 恢复 run before；无文件 candidate |
| `approve_outline` | run current Outline snapshot/ref、outline dependency、展示 ref | 无 | 字节等于 before | `outline_review→draft_review` | `outline` | mutation null、无 output | 恢复 run before；删除本 action Outline approval |
| `submit_draft` | current Outline approval、Brief/ref、bindings/Transcripts、context、workflow anchor、显式 parent ancestry、单次 rebase boundary、完整 blocks/scoped mutable IDs | `content-drafts/<content_draft_id>.json` | 字节等于 before | `draft_review→draft_review`；首次从 null 或首次合法 rebase 时切到 prepared unconfirmed child；普通同 basis child 保持 anchor | 无 | Content Draft mutation、无 output | receipt 前恢复 exact run/old anchor before并删除 prepared `content-drafts/<content_draft_id>.json`；receipt 后回读新 rebase anchor；历史 old anchor/父链不删 |
| `approve_draft` | workflow anchor、用户展示的 exact unconfirmed descendant/ref、完整 parent ancestry、Outline approval、Draft bundle、narration refs | `content-drafts/<confirmed_id>.json` → `proposals/<proposal_id>.json` | revision `+1`，active Draft 指向 prepared confirmed child | `draft_review→roughcut_review`，anchor 精确切到 confirmed child并设置 Proposal ref | `draft`，subject 为用户看到的 unconfirmed descendant，post-action pair 防立即 stale | confirmed Draft mutation、一个 pathless Proposal output | 恢复 Project/run before；删除上述两个 prepared files 和 Draft approval |
| `return_to_draft` | current Proposal/Decision exact ref、confirmed Draft ref、ancestry/bindings | 无 | 字节等于 before | `roughcut_review→draft_review` 或 `export_review→draft_review`，Proposal/Decision/Render refs 置 null | 无 | mutation null、无 output | 恢复 run before；历史 artifact 不删除 |
| `adopt_roughcut` | current Proposal/ref、Review basis、Draft approval、roughcut bundle | `edits/<edit_version_id>.json` | revision `+1`，active Decision 指向 prepared ID并清 redo stack | `roughcut_review→export_review`，设置 Decision ref | `roughcut` | Decision mutation、无 output | 恢复 Project/run before；删除 prepared `edits/<edit_version_id>.json` 和 Roughcut approval |
| `approve_export` | 第 10.4 节两次读取的完整 export basis | `renders/<render_id>.plan.json` → `renders/<render_id>.mp4` → `renders/<render_id>.manifest.json` | 字节等于 re-lock before | `export_review/active→exporting/completed` | `export` | Render mutation、MP4 与 manifest 两个相对 output | marker 后恢复 Project/run before；删除上述三项正式 candidates 和 Export approval；marker 前只删本次 staging |
| `workflow_cancel` | unique active run、空 input hash、同 action ID receipt/marker | 无 | 字节等于 before | 相同 stage `active→canceled` | 无 | null mutation、无 output | marker 前零写；receipt 前恢复 active run before；receipt 后回读 canceled receipt |

上述表中的“删除”只允许第二阶段 marker 明确拥有、动作前不存在且内容 hash 与 ref 一致的
文件；未知 orphan、历史 artifact、用户文件和不匹配内容一律保留并
`workflow_recovery_conflict`。Project/run/approval/receipt 的发布故障都不能作为成功状态
返回。

### 10.4 Render 的 staging 与提交顺序

正式 Render 是唯一允许在 marker 前离开 Project lock 的 action，因为编码前无法知道 MP4
content hash。它使用固定 transient root
`workflow/export-staging/` 和同一 Project 唯一的
`workflow/export-staging/.claim.lock`。claim lock 使用第 10.1 节相同的进程内重入保护、
POSIX/Windows advisory lock、containment、symlink/hardlink/regular-file检查，但只覆盖
本次导出的“锁内冻结 basis → staging → 重锁发布”窗口；它不替代 Project write lock。
同 action ID 或不同 action ID 只要已有进程持有 claim，都返回
`workflow_export_in_progress`，不得启动第二次 Render。

每次 staging 目录固定为 `workflow/export-staging/<staging_id>/`，`staging_id` 是
prepare 前由 core 生成的安全 ID，固定派生为 `stg_` 加既有 canonical SHA-256 的前 32 个
小写 hex。owner schema、完整 `input_hash`、`export_basis_id`、`<staging_id>.plan.json` /
`<staging_id>.mp4` / `<staging_id>.manifest.json` 命名和 recovery 校验均不变；collision
或旧 orphan 身份不一致只 fail closed，不迁移、扫描、重试或自动清理。目录内 `owner.json` 是 closed schema：

```json
{
  "schema_version": 1,
  "project_id": "proj_alpha",
  "run_id": "wfr_alpha",
  "action_id": "act_export",
  "input_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "export_basis_id": "wfb_export_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "staging_id": "stg_0123456789abcdef0123456789abcdef",
  "writing_kind": "mp4",
  "completed_files": [
    {
      "kind": "plan",
      "relative_name": "stg_0123456789abcdef0123456789abcdef.plan.json",
      "content_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  ],
  "created_at": "2026-07-28T00:00:00.000000Z"
}
```

字段全部必需、未知字段拒绝。`export_basis_id` 必须匹配
`^wfb_export_[a-f0-9]{64}$`，是 core-only opaque identity，不进入
公开 action input；`created_at` 只用于受控清理审计，不决定归属。`writing_kind` 只允许
null 或 `plan/mp4/manifest`；`completed_files` 是有序前缀，每项恰好为
`{kind, relative_name, content_hash}`，kind 顺序固定 plan→mp4→manifest，relative name
分别固定为 `<staging_id>.plan.json/<staging_id>.mp4/<staging_id>.manifest.json`，
hash 使用各自正式规则。开始写一个文件前先原子/fsync 把 `writing_kind` 设为该 kind；
文件完成并 fsync 后再原子/fsync 将其 exact ref 追加到 `completed_files` 并把
`writing_kind` 置 null。`writing_kind` 非 null 后，对应固定文件名就是该 action 明确拥有
的 partial staging：它不需要尚不可知的最终 content hash，但只有 owner identity、固定
文件名、root containment、普通文件、单硬链接及 staging 目录全部节点逐项通过时才成立。
这个字段只证明可删除内容，不报告进度。

owner、目录名、
Project/run/action/input/basis 必须逐项一致；staging 内只允许本 action 固定名称的
plan/MP4/manifest 临时文件、原子写 temp，以及下述能够精确识别的单一 core-owned
renderer workspace。损坏 JSON、duplicate/unknown schema、symlink、
hardlink、额外节点、跨 Project/run 或任一身份不符一律
`workflow_recovery_conflict`，不得删除、覆盖或重新 Render。

原生 MCP 长调用 P1 进一步冻结结果边界：renderer 合法创建的临时 workspace 必须具有
可由 closed owner、operation/action identity、staging identity 与 export basis 精确确定
的 core-owned identity；不得把 core 自己创建但无法由这些 closed 事实精确识别的目录视为
可清理节点。该 docs-only 冻结不修改本节 owner schema version。后续实现必须在不增加公开
workflow input 的前提下闭合该 identity，再允许清理；在此之前无法精确识别的临时 workspace
仍 fail closed。

OS 进程退出会释放 claim lock，但不会把 owner 伪装为完成。下一次显式
`approve_export` 先取得 claim，再检查 orphan：只有 owner 身份、受控目录、action/input/
basis 和每个列入 `completed_files` 的正式 hash 全部匹配，且没有未列入的文件时，才可能
删除该孤立 staging；已列文件已不存在仍可幂等清理。
`writing_kind` 非 null 时，只允许存在对应 fixed partial filename；在上述 owner/path/
regular-file/single-link/directory identity 全部匹配且没有额外节点时，下一次**显式**
未接入媒体 OperationRecord 的同 `action_id`、同 input `approve_export` 可以删除该
partial 与 owner staging，重新取得 Project lock、重新冻结完整 export basis，然后只启动
一次新 Render。partial 已不存在也允许幂等清理。
这不是 receipt readback 或后台 retry；basis stale 时清理后拒绝，不开始 Render。

接入媒体 OperationRecord 后，同 action ID 的 terminal record 只回读，不能再次进入
Render；failed/interrupted 的显式重跑必须使用新 action/operation ID。只有这个新的
`approve_export` 启动调用可以处理前一 action 的 tracked orphan，并且必须按以下固定顺序
和条件全部通过：

1. 在 export claim 下读取 closed owner，验证目录树、固定文件名、containment、普通文件、
   单硬链接、completed hash、Project/run 身份；若存在 renderer 临时 workspace，则必须
   恰好一个，并由 closed owner、旧 operation/action、staging identity 与 export basis
   精确确定；不得以 glob 或目录扫描猜测 identity；
2. 由 owner 的旧 action ID 与 workflow action input hash 重建 exact
   `approve_export` request hash；旧媒体 record 必须存在、属于同一 Project scope/type，
   request hash 完全相同，且状态只允许 `failed` 或 `interrupted`；
3. 在 Project lock 之外非阻塞取得旧 operation 的 media writer；取得失败表示旧 writer
   仍存活，必须保留 staging 并 `workflow_recovery_conflict`。即使取得 writer，也必须先
   保证旧 operation 的全部 core-owned media child 已停止且不能继续写入；仅有
   core/transport worker 或 lock 消失不满足该条件；
4. 持有旧 media writer 后才取得 Project/workflow lock，确认旧 action 不存在
   TransactionMarker 或 ActionReceipt，并重新构造 current Project/run/export basis；
   run、当前 export ref/dependency 和 opaque basis ID 必须与 owner 完全相同；
5. 只有上述检查全部成功才删除 exact 旧 staging；释放旧 writer 后，本次新 action 才按
   正常 preflight 重新冻结 basis并启动一次 Render。

其他 action identity mismatch、非 terminal/成功 record、缺失或不匹配 record、live
writer、仍可能写入的 media child、marker/receipt、basis 变化、symlink、hardlink、额外
节点、多个临时 workspace、临时 identity 不符、损坏内容、未知 orphan、历史 artifact 或
用户文件都必须原样保留并 fail closed。纯 OperationRecord status 和 `workflow_status`
都不执行这条清理，不扫描 artifact 猜测成功，也不自动重启、续跑或重试 worker。
清理完成后的这次**显式调用**可以重新从锁内冻结 basis 开始；core 不在后台自动重跑。

提交顺序固定为：

1. 先取得 per-Project export claim。若存在 tracked predecessor orphan，先按上段固定条件
   在 Project lock 外取得旧 media writer，再以 `old media writer → Project/workflow
   lock` 验证并清理；释放旧 writer 后，才在 Project lock 内运行第二阶段 recovery、处理
   legacy 同 action orphan、解析 `ApproveExportInputV1`，完成第 11.3 节优先级检查，读取并
   冻结 export subject/dependency、Project/run/Decision/bindings/settings/fingerprints/
   target 的完整 basis；写入并 fsync exact `owner.json`，此时不写任何业务对象；
2. 释放 Project lock；
3. 在 core 生成、仅本 action 可知且 containment 校验通过的未发布 staging 目录中构造
   Render Plan，使用原素材生成并验证 MP4，再构造 manifest；staging 文件不是 Project
   current artifact，不创建 ApprovalRecord、WorkflowRun 更新、marker 或 receipt；
4. 重新取得同一 Project lock，并先运行第二阶段 recovery；
5. 从当前 Project/run/artifact 重新构造完整 export subject/dependency/basis，必须逐 hash
   等于步骤 1；任一变化返回 `workflow_stale` 或 `workflow_subject_mismatch`，删除本 action
   staging，不发布正式 Render；
6. 此时 Plan、MP4、manifest 的 ID、内容和 hash 均已知；调用
   `prepare_export_publish` 构造完整 `PreparedExport`、approval、run after、receipt 和
   TransactionMarker；
7. marker fsync 后，按 Render Plan → MP4 → manifest → Export ApprovalRecord →
   WorkflowRun → receipt 的顺序原子发布；Project after 与当前 Project 相同，不 replace
   `project.json`；
8. receipt 发布后用 committed reconciler 删除 marker并回读结果；删除身份仍完全匹配的
   staging/owner，最后释放 export claim。

staging 阶段硬退出时 Project、run、approval、receipt 必须逐字节不变；只可能留下上述
受控、带 closed owner 的 staging，由同一 claim/orphan 规则处理。staging 完成但 basis
stale 时只删除身份完全匹配的本 action staging。marker 发布后的任何中断都进入第二阶段恢复器：receipt 不存在则恢复
exact before并删除内容身份匹配的正式 candidates/approval；receipt 存在则校验 committed
after、清 marker并回读 receipt。不得让 OperationRecord 接管 TransactionMarker 恢复，
也不得增加 worker 恢复、自动重跑或自动重新批准。Project Media OperationRecord 不替代
这条恢复边界；claim/owner 只是 transient mutual exclusion 与 orphan ownership evidence，
不是 OperationRecord、业务 action、数据库、scheduler、daemon 或持久任务状态。

TransactionMarker schema 1 在现有字段上增加以下三个必填字段：

```json
{
  "project_before": {"schema_version": 1},
  "run_before": {"schema_version": 1},
  "approval_ids": ["appr_..."]
}
```

`project_before` 必须是经现有 Project schema 完整解析的 exact payload；
`run_before` 必须是经本文 WorkflowRun schema 完整解析的 exact payload。二者 canonical
hash 必须分别等于 `project_before_hash/run_before_hash`。`approval_ids` 只列本 action
计划新建且 marker 写入前不存在的记录；`candidate_refs` 同理只列本 action 拥有的新对象。
恢复器不得接受调用方路径，也不得删除 marker 没有明确拥有的文件。

逻辑成功点是 receipt 发布。恢复策略固定为回退，不猜测继续完成：

- receipt 已存在且身份/input hash 与 marker 一致：动作已成功，只删除残留 marker 并回读
  receipt；
- receipt 不存在且当前 Project/Run 分别只处于 marker 的 before/after hash：重复安全地
  恢复 exact before，删除 marker 明确拥有且动作前不存在的 candidate/approval，再删除
  marker；调用方得到“已恢复、请重新确认”，core 不自动重跑 action；
- 当前 Project/Run 任一 hash 既非 before 也非 after：返回
  `workflow_recovery_conflict`，不得覆盖并发或外部修改；
- 恢复途中再次中断：marker 保留，下次取得锁后重复相同恢复；
- 无法确定归属的 immutable orphan 不得成为 current、批准或导出成功，也不得由恢复器
  猜测删除。

Project 与 run 不得作为匹配成功状态暴露互相不一致的组合。workflow read、status、action
和 cancel 在返回业务状态前必须先完成上述检查。

这份短期 transaction marker 只服务固定 action 的崩溃恢复，不记录历史业务事件，
成功后必须删除。

### 10.5 并发和响应丢失

- 两个进程用同 action ID/同 input：第一个提交；第二个取得锁后回读同 receipt；
- 同 action ID/不同 input：只有一个可能提交，另一个冲突；
- 两个 action ID 同时尝试当前门：只有一个提交；另一个在取得锁后按新 stage 返回
  `workflow_transition_not_allowed`；
- response 在成功提交后丢失：同 ID 重试回读 receipt；
- 进程在 marker 后崩溃：下一次 workflow read/write 在锁内回退到 exact before 或明确
  `workflow_recovery_conflict`，不自动重新执行用户动作；
- lock 获取或验证失败：`workflow_lock_failed`，不调用领域 service。

### 10.6 第三阶段 traceability matrix

| action | input schema | subject | dependency | existing service to refactor/reuse | prepare helper/result | candidates | Project mutation | run transition | approval | receipt/output | crash points | recovery result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `approve_scope` | `ApproveScopeInputV1`：opaque current/selectable scope basis + complete target ordered authorizations | core 从 target 重建第 7.1 节 Project ID、ordered source snapshots、output settings、ordered authorizations 的 `scope_snapshot` hash | `scope`：Project ID、target ordered source IDs/source snapshot hashes、output settings hash | 无领域 service | `prepare_scope_approval` / `PreparedScopeApproval` | 无 | 无，revision `+0` | 首次 `scope_review→scope_review`；首 Decision 前 `outline_review/draft_review→scope_review`，清下游 refs并保留 old Draft rebase anchor | `scope` | null mutation；无 output | prepare 后/marker 前；marker 后；approval 后；run 后；receipt 后 | marker 前零写；receipt 前 exact run before + 删除 owned approval；历史 artifact 不删；receipt 后回读 |
| `confirm_brief` | `ConfirmBriefInputV1`：opaque brief basis + exact Brief business fields + closed waiver refs | core 计算 prepared full EditBrief ID/schema/content hash；调用方不提交 subject hash | `brief`：current scope subject hash、ordered source IDs、output settings hash | `create_edit_brief` | `prepare_brief` / `PreparedBrief` | `briefs/<brief_id>.json` | active Brief prepared ID，revision `+1` | `scope_review→scope_review` 或 `outline_review/draft_review→scope_review`，清下游 refs并保留 old Draft rebase anchor | `brief` | Brief mutation；无 output | prepare 后/marker 前；Brief 后；Project 后；approval 后；run 后；receipt 后 | marker 前零写；receipt 前 exact Project/run before + 删除 Brief/approval；历史 artifact 不删；receipt 后回读 |
| `submit_outline` | `SubmitOutlineInputV1` | 完整 Outline snapshot schema 1/hash | `outline`：scope hash、Brief ref、ordered Transcript bindings/hashes、speaker basis、两个 readiness basis hash | 无领域 service | `prepare_outline` / `PreparedOutline` | 无 filesystem candidate；run 内 Outline snapshot/ref | 无，revision `+0` | `scope_review→outline_review` 或 `outline_review→outline_review` | 无 | null mutation；pathless Outline output | prepare 后/marker 前；marker 后；run 后；receipt 后 | marker 前零写；receipt 前 exact run before；receipt 后回读 |
| `approve_outline` | `ApproveOutlineInputV1` | run 当前 Outline artifact ID/schema/content hash及完整 snapshot | `outline`：scope hash、Brief ref、ordered Transcript bindings/hashes、speaker basis、两个 readiness basis hash | 无领域 service | `prepare_outline_approval` / `PreparedOutlineApproval` | 无 | 无，revision `+0` | `outline_review→draft_review` | `outline` | null mutation；无 output | prepare 后/marker 前；marker 后；approval 后；run 后；receipt 后 | marker 前零写；receipt 前 exact run before + 删除 approval；receipt 后回读 |
| `submit_draft` | `SubmitDraftInputV1` | prepared complete unconfirmed Content Draft ID/schema/content hash | `draft`：Outline hash、Brief ref、ordered Transcript bindings/hashes、speaker basis、context hash、workflow anchor + explicit ancestry；允许一处 verified old-anchor→rebase-child boundary | `create_content_draft` | `prepare_content_draft` / `PreparedContentDraft` | `content-drafts/<content_draft_id>.json` | 无，revision `+0` | `draft_review→draft_review`；首次设置 anchor；首次合法 rebase 切为 unconfirmed child；普通同 basis child 不切换 | 无 | Content Draft mutation；无 output | prepare 后/marker 前；Draft 后；run 后；receipt 后 | marker 前零写；receipt 前恢复 exact old anchor并删除 new Draft；old anchor/父链不删；receipt 后回读新 unconfirmed anchor |
| `approve_draft` | `ApproveDraftInputV1` | 用户看到且属于 workflow anchor 的 exact unconfirmed descendant ID/schema/content hash | `draft`：Outline hash、Brief ref、ordered Transcript bindings/hashes、speaker basis、context hash、anchor + complete ancestry + deterministic unconfirmed→confirmed pair | `confirm_content_draft` + `propose_content_draft` | `prepare_confirmed_draft_and_proposal` / `PreparedConfirmedDraftAndProposal` | `content-drafts/<confirmed_id>.json` → `proposals/<proposal_id>.json` | active Draft prepared child，revision `+1` | `draft_review→roughcut_review`，anchor 切为 confirmed child | `draft` subject 保持 viewed unconfirmed ref | confirmed Draft mutation；pathless Proposal output | prepare 后/marker 前；confirmed Draft 后；Proposal 后；Project 后；approval 后；run 后；receipt 后 | marker 前零写；receipt 前 exact Project/run before + 删除两 candidates/approval；receipt 后回读 |
| `return_to_draft` | `ReturnToDraftInputV1` | current Proposal 或 Decision exact ref + confirmed Draft exact ref | identity bundle：run refs、Proposal/Decision ancestry、Draft ID/schema/hash、ordered bindings | 无领域 service | `prepare_return_to_draft` / `PreparedReturnToDraft` | 无 | 无，revision `+0` | `roughcut_review→draft_review` 或 `export_review→draft_review` | 无 | null mutation；无 output | prepare 后/marker 前；marker 后；run 后；receipt 后 | marker 前零写；receipt 前 exact run before；历史 artifact 不删；receipt 后回读 |
| `adopt_roughcut` | `AdoptRoughcutInputV1` | current Proposal schema 1/2 ID/schema/content hash | `roughcut`：Draft approval hash、confirmed Draft ref、Outline hash、Brief hash、ordered Transcript bindings/hashes、Proposal ref/base Edit/full clips、Review basis ID | `confirm_edit_proposal` 或 `confirm_multi_source_edit_proposal` | `prepare_decision` / `PreparedDecision` | `edits/<edit_version_id>.json` | active Decision prepared ID、清 redo，revision `+1` | `roughcut_review→export_review` | `roughcut` | Decision mutation；无 output | prepare 后/marker 前；Decision 后；Project 后；approval 后；run 后；receipt 后 | marker 前零写；receipt 前 exact Project/run before + 删除 Decision/approval；receipt 后回读 |
| `approve_export` | `ApproveExportInputV1` | 第 7.6 节 active Decision、ordered clips/count/duration、settings、relative target、fingerprint hashes 的 export snapshot/ref | `export`：Roughcut approval hash、Decision ref、ordered Transcript bindings/hashes、settings hash、ordered fingerprint hashes、export subject hash | `create_render_plan` + `execute_render_plan`（`render_roughcut` 组合入口） | `prepare_export_publish` / `PreparedExport` | transient `export-staging/<staging_id>/owner.json` + staged plan/MP4/manifest；marker 后正式 `renders/<render_id>.plan.json` → `.mp4` → `.manifest.json` | 无，revision `+0` | `export_review/active→exporting/completed` | `export` | Render mutation；MP4 + manifest relative outputs | claim acquire；owner fsync/writing_kind；partial/complete staging；re-lock stale；marker 后；正式 plan/MP4/manifest 后；approval/run/receipt 后 | live claim 拒绝重复 render；legacy 同 action 或 exact terminal tracked predecessor（新 ID、media child 已停止、同 basis、无 marker/receipt）的 closed old record/owner/export basis/精确临时 identity 全匹配 orphan 可由显式调用清理、重新冻结 basis并只 Render 一次；其他 extra/multiple workspace/symlink/hardlink/身份不符 fail closed；marker 后 receipt 前 exact before + 删除三 candidates/approval；receipt 后回读 |

## 11. fail-closed 公开写矩阵

本节描述后续第三至第四阶段必须接入的硬门；本轮不改变现有工具。

### 11.1 必须要求 active run

以下现有公开写入口在未来接线后，无 active run 一律 `workflow_required`：

| 入口族 | 额外固定约束 |
|---|---|
| `transcribe_source` | `scope_review`；current scope approval；source 在批准范围且 ASR 被授权 |
| `brief_create` | 只可由 `confirm_brief` façade 调用 |
| `content_draft_create`, `content_draft_revise_scoped` | 语义候选只可由 `submit_draft` façade 调用；Review mechanical child 继续走第 11.2 节 |
| `content_draft_confirm` | 只可由 `approve_draft` façade 调用 |
| `content_draft_propose` | 只可作为 `approve_draft` 固定 intent participant |
| `proposal_create`, `multi_source_proposal_create` | 只可作为 `approve_draft` 固定 intent participant |
| `proposal_confirm`, `multi_source_proposal_confirm` | 只可由 `adopt_roughcut` façade 调用 |
| `proposal_reject` | run/current Proposal 必须匹配；不推进 stage |
| `render_roughcut` | 只可由 `approve_export` façade 调用 |

直接 CLI、直接 MCP、Review 按钮和 canonical Skill 最终都进入同一 application gate。
CLI/MCP 不接受 approval ID 作为权限凭据；它们提交 action/subject，core 自己解析 current
approval。

### 11.2 保持现有规则

以下操作不因旧 Project 没有 run 而失败：

- 全部 read/status/export-only read，包括 `project_open`、Transcript/Draft/Brief/
  Proposal/Decision/history/proxy read、Review snapshot 和 Markdown export；
- `project_create`；
- `source_add`、`source_metadata_update`；
- `person_create`、`speaker_map_confirm`；
- `transcript_correct`、`transcript_version_activate`；
- `proxy_create`；
- Review 内 Draft 删除、移动、从原稿加入、narration edit、candidate select、
  mechanical undo/redo；
- 现有 `edit_change`、`edit_undo`、`edit_redo` 的精确用户机械编辑语义。

这些写操作仍执行现有 revision/hash/identity 校验。唯一加性的 run 同步是第 5.2 节：
授权 ASR 发布 active Transcript、`transcript_correct` 成功激活 child 或显式
`transcript_version_activate` 成功后，只收敛同一 source 的 exact binding；不要求新
workflow action、不推进 stage、不签发 approval。
除此以外，若 active run 存在且它们真的改变某个
固定 dependency，相关 approval 在下一次 status/action 重算为 stale；不相关 revision
变化不制造 stale。它们不要求创建 run，也不加入 workflow transaction；
`ProjectStore` 只在存储层取得第 10 节同一 Project write lock，并继续执行现有
revision/expected revision 校验。后续 workflow action 在锁内重新读取 Project、run 和
固定依赖，发现变化时按 subject/dependency 返回 `workflow_stale` 或
`workflow_subject_mismatch`。

### 11.3 错误码与优先级

稳定工作流错误码：

| code | 含义 |
|---|---|
| `workflow_required` | protected write 找不到 active run |
| `workflow_run_conflict` | 同 Project 有多个 active run，或 start 与已有 active run 冲突 |
| `workflow_transition_not_allowed` | lifecycle/stage 不允许该 action 或 protected write |
| `workflow_action_invalid` | 未知 action、复数 action、字段形状非法或试图跨多门 |
| `workflow_action_conflict` | 同 action ID 对应不同 canonical input |
| `workflow_subject_mismatch` | Project/run/binding/candidate/Proposal/Decision/Review basis 不同 |
| `workflow_not_ready` | 固定 readiness 尚未满足；detail 列精确字段/operation IDs |
| `workflow_approval_required` | 当前门缺少 current approval |
| `workflow_stale` | subject 或固定 dependency hash 已变化 |
| `workflow_integrity_error` | containment、复制到别的 Project、损坏/未知 schema/ID-path 不符 |
| `workflow_lock_failed` | core 未能安全取得或验证项目锁 |
| `workflow_binding_sync_failed` | 已验证 Project active Transcript 已提交，但 façade 无法把同一 source 的 run binding 确定性同步；不得返回半状态 |
| `workflow_export_in_progress` | per-Project transient export claim 已由一个 live action 持有；不得启动重复 Render |
| `workflow_recovery_conflict` | 未完成 marker 存在，且当前 Project/Run 已偏离其 before/after，core 拒绝覆盖 |

检查优先级固定为：

1. 参数 closed-schema 与安全 ID；
2. Project/workflow path integrity；
3. active run 是否存在且唯一；
4. action ID receipt/marker 冲突；
5. lifecycle/stage；
6. subject ownership/identity；
7. readiness/approval；
8. dependency freshness；
9. 既有领域 service 校验。

不增加公开的跳过门控参数。fixture、迁移和故障注入只能从非公开 Python test harness
构造状态。

## 12. 旧 Project 兼容

- 没有 `workflow/` 的 Project 仍按现有 schema 正常读取全部 Project 和不可变 artifact；
- read、Source metadata、Draft mechanical edit、edit undo/redo 按第 11.2 节工作；
- 用户第一次进入普通写流程时，Host/Skill 必须显式调用 proposed `workflow_start`；
- `workflow_start` 创建 run 文件和目录，不修改 `project.json`，不增加 Project revision；
- 新 run 可只读检查并复用校验通过的 Source、Proxy、Transcript 和 Speaker Map；
- 不从旧 `created_by=user`、`confirmed_by_user`、Proposal/Decision、旧 MP4、manifest、
  Host task、聊天记录或文件 mtime 推导任何新 approval；
- 历史 artifact 只有在当前 run 明确选择、重新 hash 且用户经过对应门后才能成为 current
  ref；
- 旧 schema 1/2 Proposal/Decision 继续可读，不为工作流改写或迁移。

## 13. 入口响应

第三阶段可执行契约冻结内部名称、参数位置与响应形状，但仍不发布工具：

- `workflow_start(project, run_id, ordered_source_ids)`：创建 scope_review/active run；
  只允许现有 Project source IDs；新 Project 可从空数组开始，scope 批准前用现有
  `source_add` 建立候选；
- `workflow_status(project, run_id?)`：读取 current run、派生 readiness、
  `allowed_actions`、`next_action` 和 effective approval statuses；
- `workflow_action(project, run_id, action_id, action, input)`：执行第 8 节一个 action；
- `workflow_cancel(project, run_id, action_id)`：只取消 run，返回 receipt。

`project/run_id/action_id/action` 只属于 envelope；九个 action input 不重复它们。cancel
没有 input 参数并使用第 8.10 节固定空 input hash。`workflow_start/action/cancel` 各返回
对应 run、receipt（start 为 null）和重新派生的 status payload；本节把
`workflow_status` 成功 payload 冻结为以下**完整 closed schema**，不得增加、省略或重命名
字段：

```json
{
  "schema_version": 1,
  "workflow_run": {
    "schema_version": 1,
    "run_id": "wfr_alpha",
    "project_id": "proj_alpha",
    "stage": "scope_review",
    "lifecycle": "active",
    "created_at": "2026-07-28T00:00:00.000000Z",
    "updated_at": "2026-07-28T00:00:00.000000Z",
    "ordered_bindings": [
      {
        "source_id": "src_a",
        "transcript_version_id": "tr_a_1",
        "transcript_content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    ],
    "scope_authorizations": [],
    "artifact_refs": {
      "brief": null,
      "outline": null,
      "content_draft": null,
      "proposal": null,
      "decision": null,
      "render": null
    },
    "readiness_basis": {
      "scope_subject_hash": null,
      "brief_subject_hash": null,
      "required_transcripts": [],
      "speaker_resolution": {
        "mode": "not_ready",
        "refs": [],
        "waiver_subject_hash": null
      },
      "blocking_operation_ids": []
    },
    "approval_refs": {
      "scope": null,
      "brief": null,
      "outline": null,
      "draft": null,
      "roughcut": null,
      "export": null
    },
    "last_receipt_ref": null
  },
  "readiness": {
    "scope_approved": false,
    "brief_approved": false,
    "required_transcripts_ready": true,
    "speaker_resolution_ready_or_waived": false,
    "blocking_operation_ids": []
  },
  "approval_statuses": {
    "scope": "missing",
    "brief": "missing",
    "outline": "missing",
    "draft": "missing",
    "roughcut": "missing",
    "export": "missing"
  },
  "confirmation_bases": {
    "scope": {
      "basis": {
        "basis_id": "wfb_scope_4b5f41a1c552f3728bd5a95e0cae1e252027a5e3de9c13c62a8230cc6bf88d48"
      },
      "current_ordered_source_ids": ["src_a"],
      "selectable_source_ids": ["src_a"]
    },
    "brief": null
  },
  "presented_subjects": {
    "outline_ref": null,
    "draft_anchor_ref": null,
    "return_subject_ref": null,
    "confirmed_content_draft_ref": null,
    "proposal_ref": null,
    "export_ref": null
  },
  "binding_sync": {
    "state": "unchanged",
    "source_ids": []
  },
  "recovery": {
    "state": "none",
    "action_id": null,
    "receipt_ref": null,
    "message_code": null
  },
  "transient_export_claim": {
    "state": "idle"
  },
  "allowed_actions": ["approve_scope"],
  "next_action": "approve_scope"
}
```

closed/null 规则：

- `workflow_run` 必须是第 5.1 节完整 schema 1，不是摘要；
- `readiness` 恰好是第 5.4 节五个字段；
- `approval_statuses` 恰好含六个 gate，value 只允许 `missing/current/stale`；
- `confirmation_bases.scope` 为 null，或恰好
  `{basis: OpaqueConfirmationBasisRefV1, current_ordered_source_ids: [safe ID...],
  selectable_source_ids: [safe ID...]}`；current 数组等于 run bindings，selectable 数组
  等于 basis 中 Project 持久顺序，二者都不含重复 ID；
  `confirmation_bases.brief` 为 null，或恰好
  `{basis: OpaqueConfirmationBasisRefV1, eligible_speaker_waivers:
  [SpeakerWaiverInputV1...]}`。两个 basis 都由 core 派生且不持久化；Host 隐式回传
  `basis`，普通用户只看到 source/speaker 的业务名称和选择。scope basis 在 active
  `scope_review` 的首次批准/reapproval，或首个 Decision 前 active
  `outline_review/draft_review` 的显式 reapproval 可调用时非 null；brief basis 只在 active
  `scope_review/outline_review/draft_review`、scope approval current 且 `confirm_brief` 可调用时非
  null；其他状态必须为 null；
- `presented_subjects` 恰好含样例六个字段。非 null artifact ref 使用
  `ArtifactRefInputV1`，`return_subject_ref` 使用 `SubjectRefInputV1`；
  `draft_anchor_ref` 是第 5.3 节 committed anchor，不表示目录中最新 Draft；scope/Brief
  reapproval 后、rebase receipt 前返回 exact old anchor；rebase receipt committed 后返回
  exact unconfirmed rebase child；普通同 basis child 不改变该字段，`approve_draft` 后才
  返回 generated confirmed child；
  `export_ref` 由当前完整 export snapshot 派生，即使尚无 Render artifact；
- `binding_sync.state` 只允许 `unchanged/repaired`；`unchanged` 要求空数组，
  `repaired` 要求非空、唯一且按 run binding 顺序的 exact source IDs；
- `recovery.state` 只允许 `none/rolled_back/receipt_committed`。`none` 要求其余三项
  为 null；`rolled_back` 要求 action ID、null receipt ref 和
  `message_code=workflow_previous_action_recovered`；
  `receipt_committed` 要求 action ID、按第 5.5 节校验的 receipt ref 和
  `message_code=workflow_previous_action_committed`。出现 recovery conflict 时整个 status
  失败，响应不得含 `allowed_actions/next_action`；
- `transient_export_claim.state` 只允许 `idle/busy`，不暴露 PID、路径、owner 或技术 hash。
  owner 损坏/symlink/身份不符时 status fail closed，不返回这个 success object；
- `allowed_actions` 是无重复、按第 8 节 enum 顺序排列的子集；`next_action` 必须是其中一项
  或 null。所有 nested object 都拒绝 missing/unknown/duplicate key。

`allowed_actions` 是当前合法 action enum 的稳定有序子集；`next_action` 为其中唯一推荐项或
`null`。它们均为派生导航，不授权写入。

派生顺序固定使用第 8 节 enum 顺序。`next_action` 的优先级固定为：

1. `scope_review`：无 current scope approval（missing 或 stale）时只允许并推荐
   `approve_scope`；scope current 且首个 Decision 尚未发布时，`approve_scope` 仍可作为
   显式 target reapproval，`confirm_brief` 始终在 allowed actions，但 next 不因存在
   selectable extra Source 自动改为 reapproval。无 current Brief 时推荐 `confirm_brief`；
   Brief current 且 required Transcript/speaker readiness 全满足时同时允许
   `submit_outline` 并推荐它；ASR 后出现 eligible unmapped speaker 时仍允许并推荐
   `confirm_brief`，用户可保持剪辑要求不变只增加 exact waiver；若只是等待获授权 ASR，
   next 为 null。re-confirm 不增加第十个 action；
2. `outline_review`：默认推荐 `approve_outline`；`submit_outline` 可提交另一版，
   `confirm_brief` 可修改剪辑要求并返回 `scope_review`；首个 Decision 前
   `approve_scope` 可显式增加补充 Source 并返回 `scope_review`，四者都按 enum 顺序出现；
3. `draft_review`：`submit_draft` 始终允许；`confirm_brief` 可在用户明确改变
   theme/duration/focus/reorder 时返回 `scope_review`；首个 Decision 前 `approve_scope`
   可显式 reapprove 完整 target scope 并返回 `scope_review`。anchor 为 unconfirmed 时同时允许并推荐
   `approve_draft`。anchor 为 confirmed（例如 return 后）时推荐 `submit_draft`；
   Review 展示的 unconfirmed anchor descendant 可用 exact ref 调 `approve_draft`，
   façade 按 ancestry 验证，status 不扫描目录猜 latest。`next_action` 只是导航，不是
   candidate 授权；
4. `roughcut_review`：系统尚未完成 Proposal 编译时为 `null`；已有 current Proposal 时
   `adopt_roughcut`，同时 `return_to_draft` 仍出现在 allowed actions；
5. `export_review`：claim idle 时推荐 `approve_export`，同时 `return_to_draft` 仍出现在
   allowed actions；claim busy 时移除 `approve_export`、保留 `return_to_draft` 且 next
   为 null，不把“已有进程编码”误作批准或完成；
6. `exporting` 或非 active lifecycle：`null`。

## 14. 当前实现映射

本文冻结的 schema 1 控制层已经完整落地并通过主评审：

- `WorkflowRun`、`ApprovalRecord`、`ActionReceipt` 与 `TransactionMarker` 保持独立于
  `Project` schema；Brief、Content Draft、Proposal、Decision 与 Render artifacts 继续
  不可变；
- 四个 façade、九个 closed business inputs、`PreparedWorkflowCancel` 与各 action 的
  hand-written prepare/publish participant 已接到同一 application 边界；公开 CLI/MCP、
  Review 与 canonical Skills 不再绕过有限门控；
- confirmation basis、Transcript binding synchronizer、speaker waiver、Draft anchor/
  rebase、export claim/closed owner/staging/re-lock 与 exact-before rollback 已实现；
- marker-owned dict/`StagedWorkflowFile` 使用支持平台的 atomic no-replace rename/move；
  不再依赖 hardlink/unlink，WorkBuddy workspace 原生探针已验证 final single-link；
- Project-media OperationRecord 只记录 ASR、Proxy 与正式 Render 的运行状态；它不推进
  WorkflowRun，也不接管 TransactionMarker、approval、receipt 或 artifact recovery；
- W5 的失败 Render 由 TransactionMarker exact-before rollback 恢复，修复后由用户重新
  看到摘要、重新批准并使用新 operation ID 成功 Render。Host timeout 只按原 ID 回读，
  没有启动第二个 worker。

历史起点提交只保留在实施计划修订记录中；它们不再代表当前“未实现事实”。当前公开
当前工具版本以运行时 schema 为准。
