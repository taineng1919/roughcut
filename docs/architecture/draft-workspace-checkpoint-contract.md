# Draft Workspace Checkpoint Contract


配套 vectors：`core/tests/fixtures/draft-workspace-checkpoint-vectors.json`

## 1. 目的、权威性与边界

本文冻结一个最小、Review-only、schema-versioned 的 Draft Workspace Checkpoint。它只回答：

- `draft_review` 页面最后一次已向用户报告成功时，正在显示哪个 immutable Content Draft；
- 当前候选还能沿哪条已验证的 parent/child 链 undo/redo；
- 两个 Review 服务如何用同一 Project write lock 和 checkpoint CAS 竞争；
- 页面刷新、Review 服务退出或进程崩溃后，如何恢复精确成功状态而不扫描目录猜 `latest`。

本文不改变 `Project`、`WorkflowRun` 或 `ContentDraft` schema。Checkpoint 是可变的 Review
工作区指针，不是领域稿件、`ApprovalRecord`、`ActionReceipt`、`TransactionMarker` 或
`OperationRecord`，也不是数据库、event log、redo log、daemon、租约、任务队列或通用事务
框架。它不恢复跨 Project session，不自动重放编辑，不推进 workflow stage，也不证明真人
身份。

`docs/architecture/finite-workflow-contract.md` 继续负责有限 workflow 的 stage/action/
approval/transaction 语义；本文只负责 `draft_review` 内已经列入机械编辑白名单的
Content Draft child 与指针导航。两者冲突时停止实施并先交主评审，不由代码自行扩张。

## 2. 固定路径、公共类型与 canonical hash

### 2.1 唯一规范路径

每个 run 恰好只有一个 checkpoint，路径只能由 core 从已验证 Project root 与 safe run ID
构造：

```text
<project>/workflow/draft-workspaces/<workflow_run_id>.json
<project>/workflow/draft-workspaces/.<workflow_run_id>.json.tmp
```

第二行是该 run 唯一保留的原子替换临时名，不是另一份 checkpoint。调用方不能提交路径、
文件名、绝对 Project root 或任意临时目录。`workflow_run_id` 继续使用有限 workflow 的
`^[A-Za-z0-9_-]{1,128}$`；Review mutation ID 使用更窄的
`^dwop_(0|[1-9][0-9]*)_[a-f0-9]{32}$`。

Project root、`workflow`、`draft-workspaces` 与目标文件的每个现存节点都必须重新执行
containment 和 `lstat`。目录必须是真实目录；checkpoint 与 temp 必须是单硬链接普通文件；
symlink、hardlink、非普通节点、路径逃逸和未知目录节点均 fail closed。读取不得创建
`workflow/` 或 `draft-workspaces/`。

`draft-workspaces/` 只允许 safe run ID 对应的 `<run_id>.json` 和
`.<run_id>.json.tmp` 两类名字。每个 final payload 的 run ID 必须等于文件名；历史 terminal
run checkpoint 可保留只读。其他文件、子目录或无法归属的 temp 都是未知节点，不能被当前
run 读取、覆盖或删除。

### 2.2 `ArtifactRefV1`

本文所有 Content Draft ref 复用有限 workflow 的完整 ref，不新增第二套 hash：

```json
{
  "artifact_id": "draft_alpha",
  "schema_version": 1,
  "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`artifact_id` 必须是 safe ID，`schema_version` 必须为整数 `1`，`content_hash` 必须是
64 位 lowercase hex。content hash 固定使用现有
`subject_content_hash("content_draft", 1, ContentDraft.to_dict())`；不得使用文件字节 hash、
mtime、目录顺序或另造 Draft hash。

### 2.3 `CheckpointRefV1`

Checkpoint 自身的 CAS ref 不写回 checkpoint，以免产生自引用：

```json
{
  "generation": 3,
  "checkpoint_hash": "a5276921c5a000c54de851c6584b00f8418fad735e3646645eba5a66f83a6758"
}
```

`generation` 是正整数；`checkpoint_hash` 是完整 checkpoint closed payload 的
`sha256(canonical_json_v1(payload)).hexdigest()`。`canonical_json_v1`、Unicode NFC、
CRLF/CR→LF、整数、key 顺序、重复 key、float 和非法 surrogate 规则完全复用有限 workflow
契约第 6.4 节。文件末尾换行不进入 hash。上例 hash 是第 3.1 节完整 payload 的独立重算
结果。

## 3. DraftWorkspaceCheckpoint schema 1

### 3.1 完整 closed JSON

以下字段全部必需；nullable 字段必须显式为 `null`：

```json
{
  "schema_version": 1,
  "project_id": "proj_alpha",
  "workflow_run_id": "wfr_alpha",
  "generation": 3,
  "current_candidate_ref": {
    "artifact_id": "draft_current",
    "schema_version": 1,
    "content_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  },
  "project_revision": 12,
  "ordered_bindings": [
    {
      "source_id": "src_a",
      "transcript_version_id": "tr_a_1",
      "transcript_content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ],
  "context_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "redo_candidate_refs": [
    {
      "artifact_id": "draft_redo",
      "schema_version": 1,
      "content_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    }
  ],
  "last_commit": {
    "operation_id": "dwop_2_0123456789abcdef0123456789abcdef",
    "expected_generation": 2,
    "operation": "undo",
    "input_hash": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "result_candidate_ref": {
      "artifact_id": "draft_current",
      "schema_version": 1,
      "content_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  },
  "audit_review_session_id": "review_session_alpha",
  "updated_at": "2026-07-29T00:00:00.000000Z"
}
```

不接受未知、缺失、重复或类型错误字段。字段规则：

- `schema_version` 恰好为整数 `1`；
- `project_id/workflow_run_id` 必须分别等于当前已验证 Project 和唯一 active run；
- `generation` 从 `1` 开始，每次成功 checkpoint mutation 恰好 `+1`，不因纯读取增加；
- `current_candidate_ref` 是页面最后一次成功状态的完整 Content Draft ref；
- `project_revision` 是该成功状态验证过的 current Project revision；checkpoint 写入本身不
  改变它；
- `ordered_bindings` 非空、有序、source ID 唯一，逐项恰好等于 active run 的 non-null
  binding，包括 exact Transcript ID/content hash；
- `context_hash` 恰好等于锁内用当前 Project、Brief 与 ordered bindings 重算的现有 Agent
  context hash；
- `redo_candidate_refs` 可以为空；数组末项是下一次 redo，逆序遍历必须形成从 current
  candidate 开始的直接 child 链；
- `last_commit` 记录当前 checkpoint 的唯一最近成功写，不是事件历史；它的
  `result_candidate_ref` 必须等于 `current_candidate_ref`；
- `audit_review_session_id` 必须为 safe ID 或 `null`。它只说明最后一次成功写由哪个 Review
  session 发起；读取、恢复、CAS 和写权限都不得要求 session ID 相同；
- `updated_at` 使用有限 workflow 的六位小数 UTC `...Z` 时间格式，只作审计，不决定
  `latest`、所有权或 freshness。

Checkpoint 不保存 Draft 正文、blocks、Brief 副本、Transcript 文本、选择区、caret、搜索、
播放器位置、token、Host task/PID、绝对路径或聊天记录。正文和 parent 链只存在于既有
immutable Content Draft files。

### 3.2 `last_commit` closed enum

`operation` 只允许：

```text
initialize
edit_delete
edit_move
edit_insert
narration_edit
candidate_select
external_submit_advance
undo
redo
return_to_draft_reset
workflow_anchor_reset
```

这只是 Review 机械编辑的固定审计枚举，不是可扩展 action registry、工作流 DSL 或公开
CLI/MCP action。`expected_generation` 是本次成功写之前的 generation；初始化为 `0`，
其他成功写必须等于当前 generation。`operation_id` 的数字段必须逐十进制等于
`expected_generation`，从而使旧 generation 的 ID 不能在新状态下被重新解释为新请求。

## 4. 身份、ancestry 与恢复前完整校验

取得同一 Project write lock 后，读取或写入 checkpoint 必须按下列顺序验证：

1. Project、workflow/checkpoint 路径与所有节点安全；
2. `project.json` 完整解析，`project_id` 与 checkpoint 相等；
3. 有且仅有一个 active WorkflowRun，ID 与 checkpoint 相等；
4. run 必须为 `stage=draft_review/lifecycle=active`；其他 stage 只允许审计读取原文件，
   不能恢复为可编辑状态；
5. Project revision、run ordered bindings、Project active Transcript IDs/content hashes 和
   checkpoint 逐项相等；
6. current Brief 与 checkpoint `context_hash` 按现有规则重算相等；
7. 从固定 Content Draft path 读取 `current_candidate_ref`，执行完整 schema、ID、content
   hash、Project containment 和 source binding 校验；
8. 沿 `parent_draft_id` 逐项读取完整 ref，拒绝缺失、成环、跨 bindings/context、跨 Brief、
   跨 Project 或未知 schema，最终必须到达 run 当前 `artifact_refs.content_draft` anchor；
9. 按 `reversed(redo_candidate_refs)` 从 current 开始验证每项是前一项的直接
   unconfirmed child，且 Project/run/bindings/context/revision 完全相同；
10. `last_commit`、generation、current result、session audit 和 timestamp 形状一致。

步骤 5–10 是普通恢复/编辑的 current-basis 路径。若调用的是第 9.2/9.3 节两个固定 reset，
步骤 1–4 后必须先按对应 exact workflow receipt/before anchor 规则验证旧 checkpoint，再
构造并验证新的 current-basis checkpoint；不得先用“旧 revision/bindings 不等于 current”
把合法 reset 误拒绝，也不得把这个特例开放给其他 operation。

普通 candidate（包括页面 edit、Agent handoff child、undo/redo 目标）必须为 unconfirmed，
其 `base_project_revision/source_bindings/context_hash/brief_snapshot` 与 checkpoint 当前
basis 相等。

唯一特例是 `return_to_draft` 后的 confirmed workflow anchor：checkpoint 可以把这个 exact
stored confirmed artifact 作为 `current_candidate_ref`，但它只表示“从此父稿重新打开的
工作区基点”。Review 继续使用现有确定性 reopen 规则，在内存中以 current Project revision
和 context 构造可编辑视图；checkpoint content hash 始终指向未改写的 stored confirmed
artifact。它不是新的 confirmed approval，也不能把旧 checkpoint 当作当前已确认稿。
第一个机械编辑会创建以该 confirmed anchor 为直接 parent 的 unconfirmed child。

该第一个 child 提交后，ancestry 只允许一处手写的
`stored confirmed anchor → deterministic reopened basis → direct unconfirmed child`
边界：anchor 必须仍是 Project active Draft 与 run exact anchor，return receipt/current
Project revision/bindings/Brief/context 必须通过第 9.2 节校验，child 必须采用该 current
basis。之后的所有 descendants 与 redo refs 都必须逐项保持这个 current basis；不得出现
第二处 revision/context 边界。undo 回到 confirmed anchor 时再次使用同一个确定性 reopen
视图，不改写 anchor 文件。

任一步失败都不能扫描 `content-drafts/`、按 mtime/ID/目录顺序猜候选，也不能降级到 run
anchor、Project active Draft 或任意 sibling。稳定返回第 10 节错误，Project、run、
checkpoint、redo 与历史 artifact 保持不变。

## 5. Review mutation envelope、CAS 与幂等

### 5.1 内部 envelope

每个会改变 checkpoint 的 Review application 调用在进入 Project lock 前冻结：

- `operation_id`；
- `operation`；
- `expected_checkpoint_ref`（首次初始化为 `null`）；
- `expected_current_candidate_ref`；
- 既有 Review endpoint 已按 closed schema 验证的业务 input。

这些字段属于 Review application 内部接缝，不新增 CLI/MCP 或 tool schema。浏览器可以为
一次请求持有 opaque `operation_id` 并在网络重试时原样复用；普通用户不看到 ID/hash/
generation。Review session ID 不进入权限判断，也不能把另一个服务挡在租约外。

input hash 固定为：

```json
{
  "hash_schema": 1,
  "hash_kind": "draft_workspace_request",
  "project_id": "proj_alpha",
  "workflow_run_id": "wfr_alpha",
  "operation_id": "dwop_2_0123456789abcdef0123456789abcdef",
  "operation": "undo",
  "expected_checkpoint_ref": {
    "generation": 2,
    "checkpoint_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "expected_current_candidate_ref": {
    "artifact_id": "draft_child",
    "schema_version": 1,
    "content_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "input": {}
}
```

整个 closed object 经 `canonical_json_v1` 后取 SHA-256。Project revision、Review session
ID、wall clock、生成后的 child ID、绝对路径和内存 cache 不进入 input。各 operation 的
`input` 恰好为：

| operation | closed input projection |
|---|---|
| `initialize` | `{}`；candidate 只来自 run exact anchor |
| `edit_delete` | 现有 Draft selection closed payload + `accept_degraded` |
| `edit_move` | 现有 Draft selection、caret closed payload + `accept_degraded` |
| `edit_insert` | 现有 Source selection、caret closed payload + `accept_degraded` |
| `narration_edit` | `{block_id, text}` |
| `candidate_select` | `{child_candidate_ref}`，必须为 current 的 direct unconfirmed child |
| `external_submit_advance` | `{submit_draft_receipt_ref, child_candidate_ref}`；只接受同 basis、当前 exact submit receipt 的 direct child，且不得改变 Workflow anchor |
| `undo` / `redo` | `{}` |
| `return_to_draft_reset` | `{return_receipt_ref, confirmed_anchor_ref}` |
| `workflow_anchor_reset` | `{submit_draft_receipt_ref, new_anchor_ref}`；只用于已提交的 scope/Brief rebase |

未知字段在进入 hash 前由现有 endpoint/parser 或本契约 closed parser 拒绝。生成后的
Content Draft ID、正文副本、Project path 或“最新稿”提示不在 input。

### 5.2 检查优先级

同一 Project lock 内固定为：

1. envelope/operation ID/closed input；
2. path 和 checkpoint integrity；
3. active run/stage；
4. `last_commit.operation_id`：
   - 相同且 input hash 相同：回读现有 checkpoint/result，不创建 child、不增加 generation；
   - 相同但 input hash 不同：`draft_workspace_action_conflict`；
5. expected checkpoint generation/hash 与 expected current candidate exact ref；
6. Project revision、bindings、context、ancestry/redo；
7. operation-specific selection/caret/parent 校验；
8. prepare child/checkpoint after。

因此响应丢失后的同 ID/同输入重试返回同一 candidate 和 checkpoint；同 ID/不同输入不能
覆盖。两个服务基于同一 generation 提交不同 operation 时，只有先取得锁的一方提交；
后到者返回 `draft_workspace_stale`，不自动合并、覆盖或重放。

`allowed_actions`、`next_action`、`can_undo` 和 `can_redo` 都从 checkpoint/run/history
派生，不持久化为另一份真相。

## 6. 固定提交顺序与失败边界

Checkpoint 状态变化恰好为：

| trigger | before | after |
|---|---|---|
| explicit workspace initialize | checkpoint absent，run active `draft_review` | generation `1`，current=exact run anchor，redo=[] |
| delete/move/insert/narration edit | generation N，current=C | generation N+1，current=new direct child(C)，redo=[] |
| exact Agent candidate select | generation N，current=C | generation N+1，current=validated direct child(C)，redo=[] |
| undo | generation N，current=C，C parent=P | generation N+1，current=P，redo=old redo + [C] |
| redo | generation N，current=C，redo=R+[D]，D parent=C | generation N+1，current=D，redo=R |
| valid restart/readback | valid checkpoint | 字节、generation、current、redo 全部不变 |
| `approve_draft` receipt | active draft checkpoint | checkpoint 字节不变并休眠；workflow 离开 `draft_review` |
| exact `return_to_draft` reset | dormant/old checkpoint | generation N+1（缺失时 1），current=confirmed run anchor，redo=[] |
| exact rebase `submit_draft` reset | dormant old-basis checkpoint | generation N+1（缺失时 1），current=receipt committed unconfirmed run anchor，redo=[] |

不存在自动 merge、自动 redo、从 sibling 选择 `latest` 或一次 trigger 跨两个表中状态变化。

### 6.1 会创建 child 的编辑

`edit_delete/edit_move/edit_insert/narration_edit/candidate_select` 中，只有前四项创建新
child；`candidate_select` 选择既有 exact child。创建新 child 的提交固定为：

1. 取得统一 Project write lock；
2. 清理并验证本 run 保留 temp，完成第 4、5 节全部校验；
3. 只在内存中生成 child ID、完整 `ContentDraft`、full ArtifactRef、checkpoint after 与
   timestamp；prepare 不写文件；
4. 以现有 Content Draft closed schema/业务规则完整验证 child：direct parent 是 expected
   current、unconfirmed、Project revision/bindings/Brief/context 全部相等；
5. 使用内部专用原子 artifact publish 写入不可变 child：同目录受控 temp、file fsync、
   完整 schema/content-hash readback、no-overwrite rename/link publish、父目录 fsync；
   final path 只能看见完整文件；
6. 将完整 checkpoint after 写到固定 checkpoint temp，file fsync 后从 temp 严格重读并
   验证 schema/canonical hash；
7. `os.replace(temp, final)`，同步父目录；这是 workspace 的唯一逻辑成功点；
8. checkpoint 成功后才更新 Review 内存 snapshot/redo，并向用户返回成功。

所有步骤都在同一 Project write lock 内。checkpoint 不调用 `ProjectStore.save`，所以
Project revision 恒定；它不写 WorkflowRun、ApprovalRecord、receipt 或 stage。

### 6.2 只移动指针的操作

`initialize/candidate_select/external_submit_advance/undo/redo/return_to_draft_reset` 不创建
Content Draft。`external_submit_advance` 只在 Workflow 已经用 exact `submit_draft` receipt
持久化 child 后推进 checkpoint；它不重写 Workflow anchor。它们完成同样的锁、CAS、identity
和 checkpoint temp→replace→directory sync；checkpoint 成功后才更新 Review 内存并返回。
`initialize` 创建 generation `1`；其他操作 generation 恰好 `+1`。

### 6.3 失败与硬退出

- child 写入/验证失败：checkpoint 和内存不变，不报告成功；
- checkpoint temp 写入、fsync、严格 readback 或 replace 失败：旧 checkpoint 保持有效；
  当前进程只可在重新读取并核对 generated ID、direct parent、unconfirmed flag、完整
  Content Draft content hash、Project 非 active/current ref、single-link regular file 后
  删除本次新建 child；
- child 已发布、checkpoint 尚未发布时 `os._exit`：旧 checkpoint 是唯一 current；新 child
  只是不可变、未选择 orphan。重启不得扫描或自动采用它，也不得仅凭 parent/mtime 删除；
- checkpoint replace 已完成但响应丢失：新 checkpoint 已提交；同 operation ID/input
  重试按 `last_commit` 回读，不创建第二个 child；
- 清理本次 child 时发现 ID/content/parent/link identity 任一不符：保留 child 与证据，
  返回 `draft_workspace_integrity_error`；
- checkpoint 恢复或清理自身再次中断：final checkpoint 仍只有 replace 前或 replace 后
  的一个完整版本；下次重复严格验证，不存在半 checkpoint 成功态。

Reserved checkpoint temp 若在进程退出后遗留：下一次持锁读取只在它仍位于固定路径且是
单硬链接普通文件时删除；symlink、hardlink 或其他节点保留并 fail closed。未知 orphan、
历史 Content Draft、父稿、sibling 和用户文件永不删除。

## 7. undo、redo、分支与 Agent child 交接

- `undo` 只允许 current 不是 workflow anchor，且 direct parent 通过完整 ref/basis 校验；
  成功后把旧 current full ref append 到 `redo_candidate_refs`；
- `redo_candidate_refs` 的末项是下一步。`redo` 要求末项 direct parent 等于 current，
  成功后把它设为 current 并 pop；
- 连续 undo 后，`reversed(redo_candidate_refs)` 必须形成从 current 向前的唯一 child 链；
- undo 后任何新 edit、narration edit 或 `candidate_select` 成功都将 redo 数组清空；原 redo
  child files 保持不可变，不删除；
- Agent 语义改稿的 `candidate_select` 只接受调用方显式 full ref，且 child 必须直接继承
  checkpoint current、属于同 anchor/bindings/context/revision。不得扫描目录；
- `external_submit_advance` 只接受 active run 的 exact `submit_draft` receipt、完整
  persisted Content Draft ref 与 checkpoint current 的 direct parent；same-basis child
  成功后清空 redo，并把 receipt ref、child ref 和 CAS input 固定在 `last_commit`。Workflow
  anchor 保持逐字节不变；已在 current ancestry、redo 或已由相同 operation identity 提交的
  receipt 只做验证/幂等 readback；sibling、任意 descendant 和不一致 receipt 均 fail closed；
- sibling、跨 anchor、跨 run、跨 Project、不同 binding/context 或 confirmed child 都拒绝；
- undo/redo 只改变 checkpoint pointer，不改变 Project revision、WorkflowRun anchor、
  approval 或 stage。

## 8. 启动、重启与旧项目兼容

### 8.1 无 checkpoint

普通 `project_open`、workflow/status/read、Review Snapshot 和 artifact read 在 checkpoint
缺失时照常成功且不创建目录/文件。显式进入可编辑 `draft_review` workspace 时，才允许
在 Project lock 内初始化：

1. 要求唯一 active run 已经由显式 `workflow_start` 建立且当前为 `draft_review`；
2. 读取 run exact `artifact_refs.content_draft` anchor，不接受目录中其他候选；
3. 完成 Project/revision/bindings/Transcript/Brief/context/Content Draft ref/ancestry 校验；
4. 写 generation `1`、redo 空、`last_commit.operation=initialize` 的 checkpoint；
5. checkpoint 成功后才返回可编辑页面。

真正没有 WorkflowRun 的旧 Project 仍可只读打开；不得为初始化 checkpoint 静默创建 run、
修改 `project.json` 或从旧 `confirmed_by_user`/Decision/MP4 推导批准。用户要进入新的受控
编辑流程，必须先显式 start 并走到 `draft_review`。升级前已存在 active run/Draft 但没有
checkpoint 的 Project 按上述规则首次初始化。

这不撤销有限 workflow 契约第 11.2 节对旧 Project 机械 Draft 接口的读取/兼容承诺；没有
run 的旧调用仍按既有 revision/hash 规则处理，但它不伪装成 checkpoint-backed W5
工作区，也不能宣称具备本契约的跨重启 current/redo 保证。W5 和新的 Review 受控流程必须
显式建立 run 后才使用本 checkpoint。

### 8.2 Review 服务重启

服务启动只读取 checkpoint 并执行第 4 节完整校验。通过后恢复 exact current candidate 和
redo stack；新的 `review_session_id` 不要求等于 audit 字段，也不因纯读取写回 checkpoint。
失败时页面进入 fail-closed 只读诊断，不回退到 run anchor或猜 latest，不自动创建 child。

Project revision、active Transcript、Brief、bindings、context、run/lifecycle/stage 或
Content Draft ancestry 任一外部变化，都返回 `draft_workspace_stale` 或
`draft_workspace_integrity_error`；不自动迁移用户工作区。

## 9. workflow 阶段交接

### 9.1 `approve_draft`

Checkpoint 只服务 active `draft_review`。`approve_draft` 必须批准 checkpoint 当前展示的
exact unconfirmed candidate；成功后 workflow 进入 `roughcut_review`，生成 confirmed child
并切换 run anchor。旧 checkpoint 文件保持字节不变作为休眠工作区证据：

- 不把它更新成 generated confirmed child；
- 不把旧 candidate 的 `confirmed_by_user=false` 推断成当前已确认稿；
- 不在 `roughcut_review/export_review/exporting` 恢复为可编辑页面；
- 不删除旧 candidate、redo 或历史 artifacts。

这样 checkpoint 不加入 workflow action transaction，也不会破坏现有
prepare→marker→publish→receipt 边界。

### 9.2 `return_to_draft`

`return_to_draft` receipt 提交后，Review 必须把 checkpoint 确定性 reset 为：

- run 当前 exact confirmed Content Draft anchor；
- current Project revision、run ordered bindings 和重算 context；
- redo 空；
- operation `return_to_draft_reset`；
- generation 在既有有效 checkpoint 上 `+1`，没有 checkpoint 时为 `1`。

如果进程在 workflow receipt 后、checkpoint reset 前退出，下一次显式打开 Draft workspace
只在下列事实全部成立时执行同一 reset：

1. run 为 active `draft_review`，`last_receipt_ref` 指向 exact
   `return_to_draft` receipt；
2. run anchor 是 Project active confirmed Content Draft；
3. 旧 checkpoint 属于同 Project/run；
4. confirmed anchor 的 `parent_draft_id` 精确等于旧 checkpoint current candidate ID，
   或 checkpoint 已经是该 confirmed anchor 且 redo 为空；
5. receipt、Project revision、bindings、Brief/context 与当前事实完整通过验证。

前一种是唯一允许跨已知 workflow revision 变化的 deterministic reset；后一种是响应丢失
readback。其他 stale/corrupt/cross-run 组合全部 fail closed。reset 不创建 child、不重写
confirmed anchor、不签发批准；下一次机械编辑才创建 unconfirmed direct child。

### 9.3 scope/Brief reapproval 后的 Draft anchor

首个 Decision 前的 scope/Brief reapproval 会让旧 checkpoint basis 失效，但不会删除旧
Draft。第一个合法 rebase `submit_draft` receipt 已按有限 workflow 契约原子把 run anchor
切换到 prepared unconfirmed child。Review 只在以下事实全部成立时用
`workflow_anchor_reset` 把 checkpoint reset 到该 exact new anchor并清 redo：

1. run 已回到 active `draft_review`；
2. `last_receipt_ref` 是 exact rebase `submit_draft` receipt，mutation ref 等于 run
   current unconfirmed anchor；
3. receipt/before anchor 与旧 checkpoint Project/run/current ref 的 ancestry 和 rebase
   parent 完整相符；
4. new anchor 的 Project revision、new ordered bindings、Brief/context 与 current run/
   Project 完整相符。

checkpoint reset 不参与已经完成的 workflow transaction，不删除 old anchor、旧 redo 或
历史 child。receipt 前崩溃仍由有限 workflow recovery 恢复 old run anchor，所以 checkpoint
保持原样；receipt 后、reset 前崩溃由下一次显式 Review 打开重复上述验证并 reset。其他
anchor 变化一律 fail closed，不把普通 sibling 或普通同 basis child当作新 anchor。

## 10. 稳定错误码

只增加以下 checkpoint 专属错误，不建立通用错误体系：

| code | 固定含义 |
|---|---|
| `draft_workspace_transition_not_allowed` | run 非 active `draft_review`，或该固定 navigation/edit 在当前状态不合法 |
| `draft_workspace_stale` | expected checkpoint/current candidate、Project revision、bindings、Transcript、Brief/context 或合法 CAS 已变化 |
| `draft_workspace_action_conflict` | 同 operation ID 对应不同 canonical input |
| `draft_workspace_integrity_error` | closed schema、duplicate key、ArtifactRef/hash/ancestry、Project/run 身份、containment、symlink/hardlink/path 或 temp ownership 损坏 |
| `draft_workspace_write_failed` | Roughcut storage 未能写/fsync/验证/publish child 或 checkpoint；旧 checkpoint 仍有效且不得报告成功 |

错误消息必须写明责任主体和失败动作，例如：

- “Roughcut Draft workspace 拒绝覆盖已变化的 checkpoint”；
- “Roughcut core/storage 未能原子发布 Draft workspace checkpoint”；
- “Roughcut Draft workspace 拒绝恢复跨 Project 的 Content Draft ref”。

不得泄露绝对路径、token、内部 exception repr 或用户正文。

## 11. 实施边界与止损线

本阶段只冻结契约和 vectors，尚未实现：

- checkpoint domain/store 或 Review application helper；
- Review server/UI 请求字段、内存切换或错误展示；
- child 原子 publish 接缝；
- CLI/MCP/tool schema；
- OperationRecord、Host 冒烟、W5、M2.6 或 M3。

后续实现只需一个 closed value object、一个 Project-scoped store 和少量 Review application
接缝。预计 2–4 个集中工程日。若实施要求修改 Project/WorkflowRun/ContentDraft schema，
增加 daemon/租约/数据库/event sourcing/通用事务框架，或现有 immutable parent 链不能
表达第 4、7 节关系，立即停止复评。

## 12. Golden vector schema

`draft-workspace-checkpoint-vectors.json` 是标准 JSON object，顶层恰好含
`schema_version/contract/vector_schema/vectors`。每个 vector 恰好含
`id/title/setup/request/expect`，三个 nested object 的字段集合必须逐项等于顶层
`vector_schema` 声明；ID 唯一并匹配 `^DWC-[0-9]{3}$`。

Vector 中 candidate ID 是由测试 fixture 映射到 full ArtifactRef/immutable Content Draft 的
符号名，不允许生产实现按这些名字猜路径。`history` 每项恰好为
`{parent_id, child_id, basis}`，`basis` 只允许
`same/approval/return_reopen/rebase`。`request.kind` 中
`initialize/edit_delete/edit_move/edit_insert/narration_edit/candidate_select/undo/redo/
return_to_draft_reset/workflow_anchor_reset` 对应生产固定操作；
`external_submit_advance` 表示 Review open 对 exact persisted Workflow submit receipt 的
checkpoint reconciliation；`read/recover/recover_return_to_draft/recover_workflow_anchor_reset`
只表示测试 harness
触发纯读取、普通 restart 校验或第 9.2/9.3 节 deterministic reset，不是新增业务
operation。

`expect.new_child_count` 只统计本次仍可见并属于成功提交的新 child；
`orphan_child_ids` 是必须保持未选中或保留的 immutable 历史证据；
`preserve_evidence=true` 禁止测试清理该证据。所有 vectors 必须断言 Project revision delta、
workflow stage 和是否允许向用户报告成功，不能只比较错误码。
