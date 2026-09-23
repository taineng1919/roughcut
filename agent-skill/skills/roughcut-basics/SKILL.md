---
name: roughcut-basics
description: Confirm roughcut source scope, safely reuse existing results, and obtain authorization for costly local work.
---

# Roughcut basics

Use the versioned local roughcut tools. Do not edit project JSON or invoke
FFmpeg directly. Keep internal IDs, revisions, stages, hashes, receipts, bases,
refs, schemas, action names and artifact names out of normal user guidance.

## 面向用户的引导

先用对话完成素材确认，再处理准备工作和剪辑要求。用户只需要理解素材、初稿、
粗剪预览和正式导出，不需要理解内部对象或工具名称。

1. 新项目先请用户只提供每个机位素材文件夹或明确文件路径，不要求重命名原文件。推荐目录名是
   `A_主收声`、`B_副机位`、`C_副机位`；需要友好名称时只保存显示名，不改名、移动或覆盖媒体，
   尤其不破坏同名 XML、字幕或其他 sidecar。人物或内容不确定时写“待确认”，不要猜测。
2. 取得路径后，Agent 只读枚举和 probe，形成候选素材清单，回读文件清单与数量、顺序、总时长、是否有音轨、
   `内容主线/主收声机位：A / B / C / 待确认`、`主收声全程稳定：是 / 否 / 不确定`、
   ASR/代理/对轨磁盘估算、Project 所在卷可用空间和预计运行时间。用户可见的候选可写作
   “A 机（主收声/内容主线）”，但 Agent 只能按文件夹名整理候选，不能仅凭 ffprobe、文件名、音轨数量或时长
   断言主收声；ffprobe 只报告是否有音轨和技术参数。否/不确定时要求用户说明主收声变化，或先在外部整理/拆分
   Project，Roughcut 不自动切主声或混音。这两个字段只有用户确认后才成为事实，并进入现有一次素材确认摘要，
   不新增确认门。
   候选清单至少显示这些素材确认字段：`序号_场景或节点_人物或内容_机位或时间`、`文件名/路径`、`用户描述`。
   人物、内容、机位和场景信息只能由用户提供或明确确认，或在 ASR 后依据可读转录和可试听的本地证据提出建议。
   Roughcut 不得要求视觉模型，也不得在 ASR 前自动判断人物、内容、主线/补充角色、叙事价值或音质。
   不要求文件数量一一对应或用户手工配对，也不要求逐文件配对；目录枚举不递归扫描未授权位置。
3. 文件夹混放且用户未明确逐文件分组时，只保留素材候选清单，不根据文件名、数量、分辨率、音轨
   或时间戳猜机位。候选不会自动导入、转录或授权。
4. 显示名、role tags、其他 tags 和 note 必须由用户提供或明确确认。用户在对话中增删、
   替换和改名；Agent 只能整理这些用户输入，不能把建议冒充自动识别事实。未选素材只是不在
   本轮使用，不删除文件或历史记录。不重命名、移动或覆盖媒体。稳定原始目录的大文件建议 linked；宿主临时或
   缓存附件建议 copied。显示名不会重命名、移动或覆盖文件。
5. 新项目不得默认询问是否复用已有代理或 ASR。只有 `project_open` 实际发现并校验既有 artifact
   时，才说明可以复用哪些结果。代理不是默认动作；先尝试原素材播放，只有
   格式不支持、seek 失败或实际卡顿时才单独建议代理，不因 4K 或 1080p 自动生成代理。
   已有代理、转录或人物对应时才直接复用，并只说明本次不会重跑的已验证结果。
6. 路径收集与 Agent 估算可以是两轮自然对话，但把精确候选、用户确认的元数据、画面预设、ASR
   范围、耗时范围和磁盘估算合并为一次 `📌 需要你确认：确认这些素材并开始准备`。如果当前 scope 含有
   `asr:cloud` Source，这同一个确认摘要必须同时明确披露：**标记为方言/在线转录的素材会将处理后的音频发送到阿里云 Qwen
   进行在线转录，会产生网络传输、第三方云处理和相应费用。** 该披露必须在任何上传之前出现；不新增逐文件
   二次 Cloud 确认，也不新增第二套 Cloud 授权状态。若旧 scope 的批准是在 Source 还没有 `asr:cloud` 标记时
   取得的，route 变更会改变 Source snapshot 与 Project revision，旧 scope 按现有 stale/dependency 规则失效：
   必须重新读取并重新确认当前 scope，绝不能把旧的 local ASR 批准当作 Cloud 上传授权。确认后直接进入 ASR，
   不逐条重复询问是否转录；用户不填写字节数或技术预算。“继续”“可以”“往下做”等模糊回复不能
   跨越多个确认门。ASR source scope 与 speaker diarization 是两个独立维度：前者先由用户确认的
   主收声/内容主线决定，后者只是该 scope 内每个 Source 的参数，绝不能增加、替换或扩大 source scope。
   多机位先确定主收声 scope，再只对这个 scope 应用 diarization；用户已明确主/副机位时不得逐素材重复询问。
   说话人是否区分必须在首次 ASR 前一次决定：需要时第一次调用就设置 `speaker_diarization=true`，
   不得先跑普通 ASR 再重跑。代理、转录、说话人处理、组件下载和正式导出都必须明确授权；
   每次只解释当前动作、精确素材范围和可见成本。
   “进入当前 Project 的素材集合”和“进入 ASR/content scope 的 Source 集合”是两个独立集合。
   用户确认进入当前 Project 的全部媒体都必须先导入 Project；`source_add` 必须覆盖用户确认进入当前 Project 的全部媒体，
   只有已存在且精确匹配的 Source 才可复用，不能被 ASR 选择反向过滤。`source_authorizations`/`transcribe`
   只能决定已经导入的 Source 中哪些进入内容/ASR，绝不能反过来过滤 `source_add`。
   例如用户确认 4 条主轨 + 4 条副轨 = 8 条项目素材、仅 4 条主轨做 ASR 时，必须满足：

   ```text
   confirmed media = 8
   Project Sources = 8
   ASR/content Sources = 4 main
   aux Sources = 4
   ```

   执行语义是 `source_add × 8`、`transcribe × 4 main only`；`aux × 4` remain in Project, no ASR。
   新 Project 与已有 Project 的 import readback 规则不同：新 Project 必须把本次确认进入 Project 的全部媒体逐一导入，
   一条不能漏；已有 Project 只要求本轮确认媒体全部存在且 exact match，可以保留不参与本轮任务的历史 Source。
   历史/额外 Source 不得自动进入当前 ASR/content scope 或机位授权。
   多机位项目中，只有用户确认的主收声/内容主线机位进入本轮内容整理和 ASR；副机位必须先作为 Source 导入 Project。
   副机位虽已导入 Project，也只用于后续声音对轨与平行画面输出；不做 ASR，也不进入内容整理、提纲、初稿或成片内容决定；
   副机位声音对轨直接读取原 Source 音轨，
   不需要副机位 Transcript。不得声称副机位转录是自动选镜、自动切机或声音对轨的前置条件；当前产品不提供自动选镜或切机。
   副机位不得出现在 `source_authorizations`，即使 `transcribe=false` 也必须 fail closed；
   副机位只通过已确认的 `multicam_setup` 和显式 exact `source_pairs` 进入机位 setup，不能用
   文件名、index 或隐含顺序补绑定，也不产生 transcript/readiness 要求。
   路径收集和估算仍是两轮自然对话，但正式素材确认门只有这一次。
7. 新项目同时确认一个普通项目名称。Agent 在安装配置的系统用户视频目录下使用独立的
   `Roughcut Projects/<项目名称>` 工作区；不要让用户像 coding 一样预建仓库，也不要把
   原素材目录当作 Project。稳定目录或外置硬盘中的大媒体继续 linked，只读且不复制；
   处理期间必须保持挂载路径稳定。若用户改选 Project 位置，先说明代理、输出和缓存可能
   占用的空间，再使用其明确批准的位置。

## 长任务反馈

对 ASR、获批代理和正式导出，宿主暴露 Roughcut MCP 时使用宿主管理的原生 MCP
task/session；只有入口 Skill 已判定 MCP 不可用且安装诊断给出绝对 CLI 路径时，才使用
CLI 进程/session。原生 MCP 的 schema 校验或工具调用失败不等于 MCP 不可用，必须停止并
报告，不得改走 CLI。ASR/Proxy 启动前由宿主生成并保留 `op_` 加 UUID v4 的 32 位小写
十六进制 operation ID，再把该 ID 原样提交给工具；同时保留 task/session 句柄。开始前用 `⏳ 正在处理`
报告素材范围、预计耗时范围和磁盘估算；运行中
只报告进程存活、已耗时和当前步骤。没有可信百分比 API 时严禁伪造百分比。完成后用
`✅ 已完成` 报告结果、实际耗时、产物和清理状态；失败用 `⚠️ 需要处理` 说明下一步。
每个长任务结束后从版本化工具响应做最终单一 JSON readback。

若宿主临时 task/session ID 失效，但已持有 Project-media operation ID，只调用一次
`media_operation_status(project_path, operation_id)` 回读；不得自行 `sleep` 或轮询失效
task；不得扫描 Transcript、Proxy、Render、staging、receipt 猜成功，也不得自动重启或
重试。只按 record 的 exact status 使用以下固定首句：

- `pending`：`⏳ 等待开始：任务已记录，媒体处理尚未开始。`
- `running`：`⏳ 正在处理：任务仍在运行。`
- `succeeded`：`✅ 已完成：任务已成功完成。`
- `failed`：`⚠️ 需要处理：任务执行失败。`
- `interrupted`：`⚠️ 已中断：执行进程已经结束，任务没有成功终态。`

只有 `succeeded` 才能继续报告 record 的 exact result；`failed/interrupted` 停止，不
自动创建新 ID。若宿主既没有进程/session 也没有 operation ID，必须用
`⚠️ 需要处理` 明确说明无法可信回读，不能从 artifact 推断。

## 仅供 Agent 执行的顺序

### WP1 `asr:cloud` 路由标记与显式声明（仅 Agent 执行）

- 自动 marker 只在 `source_add`/import 登记时判断原始输入文件名：只有
  `Path(original_input_filename).stem.endswith("__方言")` 才写入 `asr:cloud`。判断必须使用原始输入 basename，不能使用
  resolved target filename、可变 `display_name`，也不能做任意包含“方言”的 fuzzy match；marker 不得改写
  `display_name`，不得重命名、移动或覆盖原素材。导入后只读已写入的 canonical tag，不从后来改名的
  `display_name` 重新推导 route。
- 用户明确说某个 Source“有方言”或“走在线转录”时，按同一个 `asr:cloud` marker 处理；移除 marker
  只能在用户明确要求 remove 时执行。对已导入 Source，先 `project_open` 回读当前 `display_name`、`note`、全部
  `tags` 和 Project revision；保留全部现有 metadata，只 merge 或明确 remove `asr:cloud`，然后通过既有
  `source_metadata_update` 以完整 tags replacement 和 current expected_revision 写回。省略 `display_name` 以保留当前名称，
  不要提交只含 `asr:cloud` 的 tags，也不要因 Cloud、credential 或 provider/network failure 自动删除 marker。
- 每次 `source_metadata_update` 成功后都要 `project_open` readback；后续 Source 使用该回读或 mutation response
  返回的最新 Project revision。遇到 stale 或 revision conflict，立即停止并遵循既有只读诊断，不重放旧写入，
  不用另一种 payload 探测。
- 用户说“这些/这批全部在线识别”时，只处理当前明确批次；用户说“当前项目现有素材全部在线识别”时，
  才处理当前 Project 已有 Sources。两种语义都必须逐 Source 按顺序执行 read → preserve → merge/remove only
  `asr:cloud` → complete write，并在每次成功后继续使用最新 revision；不得创建 `always_use_cloud`、
  `default_backend`、Project-level persistent Cloud default，或让未来新导入素材自动继承 marker。
- 当前 WP1/WP3A 登记的是 canonical routing metadata 与本地 credential readiness：`asr:cloud` 只表示 route，
  不等于上传授权，也不等于已经上传。credential 的配置与 readiness 由 WP3A 的
  `qwen_credential_configure` / `qwen_credential_readiness` / `qwen_credential_clear` public surface 提供，
  且必须按下一节的 lazy 规则在 Cloud scope 边界自动读取一次。Cloud execution 已接通：当前 scope 真正开始
  Cloud transcription 时，Agent 不再因为“执行链未接通”而停止，而是按下面的固定规则使用**同一个**
  `transcribe_source`。

### WP3A Qwen credential 配置与 readiness（仅 Agent 执行）

- **Lazy automatic readiness（Cloud scope 边界）**：当且仅当当前显式 ASR/content scope 含有 `asr:cloud`
  Source，且 Agent 正在准备该 Source 的 Cloud transcription 时，Agent 必须先做一次
  `qwen_credential_readiness` 本地读取，再继续准备动作或报告停止；这是 readiness 的唯一自动触发边界。
  以下情况一律不检查、不要求、不报告 credential，也不因 credential 缺失而失败：
  `roughcut health`、普通 `diagnostics`、local FunASR/Paraformer transcription、没有 `asr:cloud` Source 的
  Project、`project_open`、metadata edit，以及 render/review/NLE。credential 不得成为全局启动门
  或任何 local 流程的前置条件；没有 `asr:cloud` Source 的普通 local 工作流不检查 credential，
  不把 Cloud readiness 当成 health gate。
- 只有用户明确说“配置 Qwen API”“更新在线转录凭据”“查看 Qwen 凭据是否配置”这类请求时，才主动调用
  `qwen_credential_configure` / `qwen_credential_readiness` / `qwen_credential_clear`；这两个触发条件之外的
  其他请求不调用这三个入口。
- 用户可以在 Agent conversation/tool request 中直接提供 API Key + Workspace ID，这是 V1 已接受的便利边界；
  但 Agent 不回显、不回抄、不在总结、错误说明、诊断或任何文本中重复该 key，也不把 key 写入 Project、
  `runtime.json`、Source metadata、note、Transcript、日志或普通 evidence。
- 写入只通过 `qwen_credential_configure` 一次完成（两个字段必须同时提供）；成功后 readback 只看
  non-secret 的 readiness 字段，不要求、不展示 key 的原文。Agent 不得用 CLI argv、环境变量或手工写文件
  绕过该入口，不得为本入口自行构造 `--api-key` 之类参数；API Key 不得含控制字符、换行/制表符或其他不可见字符
  （Core 会以 `invalid_arguments` 拒绝）。
- readiness 的 `status` 为 `not_configured` / `configured` / `invalid` / `insecure`。`invalid` 或
  `insecure` 时向用户说明当前凭据不可用并建议重新配置，不得自行修改文件权限、重新写入或删除其他
  Roughcut 配置；`qwen_credential_clear` 只在用户明确要求移除凭据时调用，且只删除 Qwen credential 记录。
- readiness 是纯本地读取，不代表 provider 已接受该 key。`configured` 只是本地状态：它既不代表 provider 会接受
  该 key，也不绕过当前素材确认门，更不能替代上面那次带第三方上传/费用 disclosure 的 scope 确认。
- 在自动边界读到 readiness 后，Agent 按 status 向用户报告当前状态的下一步；`not_configured`、`invalid` 或
  `insecure` 不是 transcription 失败，不得据此创建 `transcription_failed` MediaOperation、删除 marker 或宣称
  已经上传。
- 若当前 ASR/content scope 含有 `asr:cloud` Source，Agent 必须按以下固定顺序处理该 Source，不增加第二个 public API、
  不增加任何 Cloud flag，也不增加逐文件二次确认：
  1. 先按上面的 lazy 规则完成一次 `qwen_credential_readiness` 本地读取，并把 non-secret 状态作为报告的一部分；
  2. readiness 为 `not_configured`、`invalid` 或 `insecure` 时，停止该 Source 的转录并进入 credential setup/readiness：
     向用户索取 API Key + Workspace ID，用 `qwen_credential_configure` 写入并只回读 non-secret readiness；成功后
     继续当前**已确认**的 Cloud scope。只要 scope 与 revision 没有变化，不额外制造第二个用户确认门；此时不创建
     transcription operation，不 fallback local FunASR，不声称已经上传；
  3. readiness 为 `configured`，且当前 Cloud scope 已完成带第三方 disclosure 的现有素材确认时，像普通素材一样调用
     **原有的 `transcribe_source`**（带宿主生成并保留的 operation ID）。Agent 不需要知道也不需要传内部 backend 参数，
     因为不存在这个参数；Core 自己按 Source 的 canonical route 解析出 Cloud 执行；
  4. 不 fallback 到 local FunASR 或其他 local backend 顶替；
  5. 保留该 Source 的 `asr:cloud` marker，不因 credential、provider 或 network failure 移除；
  6. provider/network 失败时只按 operation record 的 exact status 用 `⚠️ 需要处理` 报告责任主体，不自动重试、
     不继续轮询旧 remote task、不复用旧 operation ID 重跑；用户的显式重跑必须重新读取当前 basis 并使用新的 operation ID。
  这是报告与执行条件，不是静默跳过、静默替换 backend 或静默修改 scope 的许可；也不得为此重放旧写入、改用
  另一种 payload 探测或直接修改 Project JSON。本规则只定义 WP1 metadata routing、WP3A readiness 与 WP3B Cloud
  execution 接线，不增加第二个 public API。

Gate 阻塞诊断遵循以下总原则：`workflow_status` 的 `allowed_actions`/`next_action` 为空或 gate
不满足，首先是诊断事件，不是“寻找合法 action 把状态变绿”的许可。Core hard gate 只负责机器可证明的
identity/revision/artifact/state 不变量；scope 业务变更、speaker waiver、批准/采用均属于明确用户决定；
完整阅读、解释 blocker、向用户报告、启动并验证 review 服务属于 Agent orchestration。

阻塞必须先分类为数据未就绪、Agent 执行错误、Core 缺陷、需用户决策四类。不得为了满足 gate
静默改变已确认业务事实、scope、有序 source authorization、speaker waiver 或其他用户决策。仅当属于
Agent 执行错误且机械纠错不改变用户业务意图时，可做一次不改变意图的机械纠错（如重算 hash、重读最新
status、以正确 offset 重试分页）；若纠错会改变已确认事实、scope 或 waiver，必须先向用户报告当前阻塞
原因、影响和可选决定，取得明确决定后再执行。首个 Decision 前的合法 scope reapproval 保留：若发现旧
scope 错含副机位，Agent 必须先向用户说明将撤回错误范围，再以同一 `approve_scope` 纠正；不得粗暴禁止所有
reapproval，也不得为满足旧 scope 先转录副机位。

任何 state-changing workflow action 失败后，立即进入“只读诊断阶段”，而不是把失败当作 validator。根因未确定前，
严格禁止新的 state-changing action、换 `action_id`、缩 payload、构造 dummy/minimal/single-block candidate、
通过“先写进去看看”探测，或直接修改 Project JSON、改变业务对象。诊断阶段后续动作仅限
`workflow_status`、`project_open`、现有 read-only 工具，以及保存并分析 exact error。只读重算 hash、重新读取
current status、以正确 offset 继续分页等不改变业务事实的机械纠错，各只允许执行一次。

只读证据确定根因后，若确认是业务意图不变且安全的机械纠错，才允许执行一次正式恢复动作；若涉及新的用户决定，
必须先说明根因与影响并取得明确决定，再允许执行一次正式恢复动作。恢复动作是正式执行，不得继续被当作 validator，
也不得用第二次写入探测另一种 payload 或业务对象。若只读证据不能证明恢复安全，则停止并报告。保留现有合法
scope reapproval 语义；scope 修正仍须先说明错误范围及影响、取得用户决定，再以当前 basis 执行一次正式恢复。
不增加通用 preflight API、不增加“失败计数”状态、不扩 Core hard gate。

以下工具名属于执行说明，不得作为用户导航步骤。

1. 宿主暴露 Roughcut MCP 时，先原生调用 `health`，不得搜索 PATH、源码或裸 CLI。首次
   安装后的第一个业务项目还要原生调用一次 `diagnostics`；它必须指向已批准的
   external/managed runtime，不能是仓库 `.acceptance`、fixture 或其他项目的临时环境。
   `health` 只做快速 core 检查，不能替代组件诊断。已有项目调用 `project_open`，读取 Project revision、当前输出设置、
   现有 Source 和活动 Edit 状态；保留本次读取的 revision。只有 `project_open` 实际发现
   并校验既有 artifact 时，才可复用既有代理或 ASR。新项目不要先调用 `project_create`。
2. 用宿主的已有本地文件能力建立候选清单。用户确认精确素材清单和 `output_preset` 后，
   新项目才调用 `project_create` 并 readback；`output_preset` 只可为
   `landscape_1080p` 或 `portrait_1080p`。已有项目只回读当前 Project settings，不询问
   或尝试更改画面预设。新 Project 必须对本次确认进入 Project 的全部媒体逐一完成导入，一条不能漏；已有 Project
   只逐一核对本轮确认媒体是否已有且 exact match，缺失才调用 `source_add`，不匹配则停止并报告，不改绑到别的 Source。
   允许保留已有 Project 中不参与本轮任务的历史/额外 Source，不删除、不移动、不重命名。导入/复用循环只覆盖本轮确认
   的 Project 媒体，不能只遍历 ASR/content scope；副机位也必须先导入。
3. 每次 `source_add` 成功后调用 `project_open` readback，再用新 revision 继续。对用户
   确认的显示名、tags、note 调用 `source_metadata_update`；成功后再次 `project_open`
   readback。不得修改 locator、fingerprint 或原文件。
4. 正式 `workflow_start` 前必须完成一次已有 `project_open` 的强制 Agent readback，不新增 Core gate：
   `已确认 Project media` → 全部 `source_add`/精确 Source 复用完成 → `project_open` → 核对本轮确认媒体
   的存在性与 exact match → 才允许 `workflow_start`。新 Project 的 readback 必须确认本次确认媒体全部已导入，一条不能漏；
   已有 Project 的 readback 允许存在不参与本轮任务的历史/额外 Source，只要求本轮确认媒体全部存在且 exact match。
   不要求 `project.sources` 全局顺序与本轮确认清单完全相同；只核对本轮媒体的 membership 与 exact match。
   历史/额外 Source 不得自动进入当前 ASR/content scope、`source_authorizations` 或机位授权；当前 scope 顺序只来自本轮确认。
   在 4 主 + 4 副的新 Project 示例中，readback 必须确认 `Project Sources = 8` 后才允许 `workflow_start`。
   如果本轮确认媒体缺失或 exact match 失败，停止并报告前序导入/回读不一致，不能继续 workflow。素材导入和元数据确认完成后，
   为本次工作生成并保留一个安全的 run ID，调用 `workflow_start`；已有 active run 则只调用 `workflow_status`，
   不得静默新建第二个 run。用户确认完整有序素材范围和逐素材转录/说话人处理选择后，从 `workflow_status` 回传
   core 给出的当前素材确认依据，并只调用一次 `workflow_action(approve_scope)`。Agent 不计算确认 hash，
   不从聊天或已有 Transcript 推导授权。动作后读取 receipt 和 `workflow_status`；stale、范围不一致或 action
   不在 allowed actions 时停止，只问一个明确问题。
   多机位时，这里的内容 scope 只列主收声/内容主线 Source；副机位虽已导入 Project 并保留用户确认的
   机位分组授权，但不得加入 `source_authorizations`，不得为满足 `required_transcripts_ready` 把它改成
   `transcribe=true`；副机位不进入 Brief、Outline、Draft 或任何内容决定。声音对轨直接读取授权的原 Source 音轨，不依赖副机位 Transcript。
   副机位的 setup 只接受 `multicam_setup` 与 exact `source_pairs`；若误把副机位放进
   `source_authorizations`（包括 `transcribe=false`），立即停止并回报 Core 的 fail-closed 结果。
   若首个 Decision 前发现当前 scope 已误含副机位，先向用户说明将撤回错误内容范围，再从最新
   `workflow_status` 原样回传新 scope basis，以同一 `approve_scope` 显式改为仅主收声 Source；不得为
   满足旧 scope 先转录副机位。若 status 不再允许 `approve_scope`，停止并报告，不能改 Project JSON。
5. 每个高成本动作前都重新 `project_open`，以当前 revision 和精确 `source_id` 范围展示
   计划。`proxy_read` 只用于发现可复用的 ready 代理，不能判断原素材是否需要代理。
   没有 ready 代理时默认继续使用原素材；只有真实 Review 播放/seek 失败或用户明确反馈
   卡顿，并且用户批准成本后，才先生成并保留 operation ID，再带该 ID 调用
   `proxy_create`，成功后再用 `proxy_read` 和
   `project_open` readback。
6. 对每个已选 Source 先调用 `transcript_versions_read`。已有项目存在可解析的活动
   Transcript 时，绑定其精确 `transcript_version_id` 并复用；新项目在素材确认门后先
   生成并保留 operation ID，再带该 ID 调用 `transcribe_source`，随后
   `transcript_versions_read` 和 `project_open` readback。
   多人素材且用户要求区分人物时，必须先从 `diagnostics` 确认 speaker 模型 ready，并为
   本轮调用显式设置 `speaker_diarization=true`；缺失时停止到组件 plan/批准/apply，不得
   先跑无 speaker ASR 或从画面/文字猜人物。已有兼容 external speaker 只读复用。
   此处“已选 Source”只指当前内容 WorkflowRun binding；多机位副机位不在此循环中，不调用
   `transcribe_source`。`speaker_diarization=true` 只对已选主收声 Source 生效，不得因开启说话人
   区分而把副机位加入授权或调用 `transcribe_source`。
7. 说话人处理必须显式 opt-in：ASR 完成报告在同一处列出每个 local speaker 的临时编号、Agent
   建议的人名或身份、2–3 条代表性原话和可读时间位置；用户可以在同一对话中确认、改名、改身份
   或保留未映射。用户提供但原声未出现的姓名标注“用户提供，原声未出现姓名”。随后先用
   `people_read` 回读已有结果；只有用户要求且有当前 Source + Transcript 的局部证据时，才调用
   `person_create` 与 `speaker_map_confirm`。相同 local speaker ID 不得跨 Source 或 Transcript
   合并；每次写入后调用 `people_read` 和 `project_open` readback。
8. 用户明确专名错误时只问：“是否同时把文稿中的‘蔡鑫’校正为‘蔡欣’？这不会重跑识别，也不会
   改变时码？”同意后使用既有 `transcript_correct` 创建并自动激活不可变 Transcript 子版本；不需要再调用
   `transcript_version_activate`，不重跑 ASR，不修改 ticks、segment identity、original_text、fine units
   或 speaker evidence。
9. 每次转录完成或活动 Transcript 切换后调用 `workflow_status`，只使用 core 同步后的
   exact binding；不得自行改写 run、推进步骤或签发确认。
10. 任一 state-changing action 返回 stale、revision conflict 或其他失败时，立即进入上面的只读诊断阶段；丢弃旧
   revision，重新 `project_open` 并向用户说明当前步骤需要重新打开。诊断期间不得重放、合并、覆盖或用新的写入探测
   gate。只有只读证据确定安全机械纠错，或用户已明确作出所需新决定后，才允许一次正式恢复动作。

`fake_project_roundtrip` 仅用于契约检查，不能代替用户项目或媒体验证。优先使用
`references/tool-contract.md` 描述的本地 stdio MCP 工具。只有宿主没有 MCP 且安装诊断
已经返回绝对 CLI 路径时，才使用该绝对路径执行 `<command> --json`；不要调用 PATH 中的
裸 `roughcut`。
