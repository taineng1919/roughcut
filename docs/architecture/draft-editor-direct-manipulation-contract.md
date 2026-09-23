# Draft Editor Direct Manipulation and Section Structure Contract


This contract defines direct draft editing, section structure, result selection, and display punctuation behavior.

## 正文段落、标题、章节命中区与有界性能

本轮只修复现有 schema 2 编辑视图、prepare 投影和 Review 直接操作，不改变 Content Draft、Proposal、
Decision、workflow action/error、exact refs、trusted boundary pair、drop envelope、CAS、checkpoint、
history 或 operation ID 语义。

- 每个独立 `section_title` 在正文中恰好显示一次；同名、连续、末尾和空章节按 block identity 独立保留。
  章节成员身份、段落归属、标题显示边界是三个独立的私有临时投影概念，不得再用一个
  `starts_section` 同时表达三者。不得按标题字符串去重、伪造空内容、持久化 paragraph artifact、
  新增第二套章节状态或原地修改 schema 1。
- 段内词语/句子移动只改变该段内部顺序；成功 child 重新加载后段落数量不增加且不强制换行。部分文字
  移出后，来源剩余前后内容仍属于同一原段落；移入内容在目标 caret 处并入目标段。source block/ref
  拆分不得重复章节标题；narration 只有真正紧邻章节起点时显示标题，章节中部不重复。选区跨越两个或
  更多显示段时保留其内部段落边界；落在目标段中部时，目标段按 caret 保留前后两个自然边界，不把移动
  内容和目标前后内容压平成一个段落。
- 章节页不启用整行拖动。只有左侧专用拖动带可以建立 reorder，命中区约 40 CSS px 宽、贯穿整行高度，
  且不小于 32×32 CSS px；六点图标居中且永久可见，命中区显示 `grab/grabbing`。标题/数量单击仍
  返回正文并定位，“…”菜单不触发拖动；pointerdown 只有来自拖动带时才建立 section drag。不增加
  常驻上移/下移按钮或“移动到…”菜单。
- 长稿基准至少包含 10 个长中文段落、3 个非空章节和 1 个空章节，并分开记录
  `selection resolve`、`pointermove hit-test`、`drop HTTP/server`、`DOM patch` 四段耗时。
  `pointermove` 零请求；正文插入线使用独立 overlay，相同 target 不重建；fallback 使用有界命中，
  不逐字符同步读取整段 Range geometry。章节拖动开始时缓存行 identity/geometry，target 不变时不重复
  扫描 DOM 或重建插入线；不使用 timeout、轮询或 RAF 竞速。
- 真实长正文拖动的 `pointermove` p95 目标不超过 16 ms，且不得出现由本功能产生的超过 50 ms long task。
  未达到时必须保留 trace 并停止，不以主观“变快”结案。工程自动化兼容矩阵覆盖 Chromium/WebKit 的
  Selection、Range、Pointer Events 差异；本机没有 WebKit 时只记录未验证，不安装或下载浏览器。项目
  发起人只需在自己使用的一个受支持 macOS 稳定浏览器完成一次最终 UX 验收，不宣称支持任意旧浏览器。

## 1. 范围和不可变约束

- Review 页面继续是左侧初稿、右侧原始转录稿的左右两栏布局，不增加第三栏、常驻侧栏、浮动
  工具箱、完整 Word、传统 NLE、通用 workflow、daemon、queue 或 retry。
- 左侧顶部提供紧凑的“正文｜章节”互斥切换。正文模式显示连续文稿；章节模式以章节列表
  替换左侧正文。右侧原稿栏不因切换而成为第三种写入面。
- 正文模式顶部不显示第二份“结构概览”或编号章节链接；章节模式是唯一目录/结构操作入口。历史
  修订中出现的结构概览只作为已被现行章节模式取代的记录，不是当前验收项。
- Timed Transcript、Content Draft parent、Proposal、Decision 和 Render Plan 均保持不可变、
  可追溯和向后兼容。成功写操作只创建一个 immutable Content Draft child。
- 原素材和原始 Transcript 不改变。来源同期声的 exact refs、ticks 和 core 派生的 canonical
  text 不得改写、补写或漏写。
- display-only 标点或空白造成 trusted fine units 的时间间隙时，同一个视觉 caret 必须解析为
  `left_end_ticks/right_start_ticks` 边界对；拆分左片只使用前者，拆分右片只使用后者。不得把任一侧
  tick 复制到另一侧、给显示字符伪造时间或创建跨越该间隙的新 ref。
- 正文拖放与章节结构编辑共用一个 Draft Workspace Checkpoint、同一 undo/redo 历史和同一
  单写者/CAS 边界；两个模式互斥，不能同时进入两个写操作。

## 2. Content Draft schema 2 的单一章节真相

### 2.1 最小模型

schema 2 保留一个有序 `blocks` 序列，并在该序列中新增独立标题项。以下 JSON 只冻结合法的
`blocks` 片段，不冒充省略其他必填字段后的完整 Content Draft artifact：

```json
[
  {
    "block_id": "section_intro",
    "kind": "section_title",
    "title": "开场"
  },
  {
    "block_id": "block_quote_1",
    "kind": "source_excerpt",
    "refs": [
      {
        "source_id": "src_a",
        "transcript_version_id": "tr_a",
        "segment_id": "seg_1",
        "start_ticks": 0,
        "end_ticks": 120000
      }
    ],
    "canonical_text": "来源原话"
  }
]
```

除本节所列变化外，Content Draft 的 project/basis、parent、Brief、bindings、context、确认状态、
source excerpt 和 narration 字段沿用既有契约。

- `section_title` 是有稳定 `block_id` 身份的独立有序项；`title` 是 trim 后 1–80 个 Unicode
  code points 的用户可见文字。
- schema 2 的 `source_excerpt` 和 `narration` 项禁止出现旧的内嵌 `section_title` 字段。
  章节顺序、边界和名称只由独立标题项决定，禁止持久化或派生第二套章节真相。
- 标题项可连续出现，也可位于序列末尾；从一个标题项到下一个标题项或文稿末尾之间没有
  内容时，该标题项表示一个真实空章节。
- 第一个标题项之前的普通项属于无标题前言。没有标题项的 schema 2 draft 仍是合法文稿。
- 同名标题按各自 `block_id` 独立；不得按 `title` 字符串合并、去重或寻址。
- 标题项不生成 clip、时长、同期声、recorded refs、媒体占位或 Proposal item。它只参与
  Content Draft 的结构和显示。
- narration 仍是不可拆分的完整块；其选择和拖放规则见第 3.2 节。

### 2.2 schema 1 只读兼容与首次编辑

- 已有 schema 1 artifact 原样、只读保存并继续可读取；不得原地迁移、重写或补发同 ID 文件。
- Review/editor 或任一 child-producing 路径读取 schema 1 进入逻辑编辑视图时，每个普通 block 的
  旧 `section_title` 确定性解释为紧邻该 block 之前的逻辑标题项；公共 `content_draft_read` 仍按
  原 artifact schema 返回，不应用该投影。投影身份使用 `legacy_section_` 加以下字节串 SHA-256
  的前 32 个小写 hex：
  UTF-8(`content_draft_id`) + 单字节 NUL + UTF-8(`block_id`) + 单字节 NUL + ASCII 十进制 block
  index（无符号、无前导零）；不得依赖标题字符串、平台路径、locale 或读取时间。
- `legacy_section_<32 lowercase hex>` 必须满足现有 SAFE_ID。若投影 ID 与任一不同项的持久 block ID、
  同次投影的另一标题 ID 或 schema 2 child 中另一待发布 block ID 冲突，逻辑编辑视图构建/首次写入立即 fail
  closed；禁止静默别名、加后缀、覆盖或按标题字符串重新分配。公共 `content_draft_read` 不构建投影，
  仍可原样读取该 schema 1 artifact。该冲突沿用既有 artifact/integrity 失败边界，不新增公开 error。
- 只读投影不写 artifact、不更新 Project、Workflow Run 或 checkpoint，也不声称 parent 已是
  schema 2。schema 1 的其余字段继续按原契约读取。
- 对 schema 1 parent 的第一次成功编辑必须把完整逻辑序列固化为一个 schema 2 child：保留所有
  既有普通 block ID 和字段，将逻辑标题身份固化为 schema 2 `section_title.block_id`，再在同一
  原子 prepare/publish 中应用恰好一个用户 operation。失败时 parent 和所有指针零变化。
- 新建稿、Agent 新 child 和 Review 机械编辑在 schema 2 上只写独立标题项；不得同时写旧
  `source_excerpt.section_title` 或 `narration.section_title`。

该模型无需虚假正文、虚假解说占位、原地迁移、长期 preserve lock 或两套章节来源；若实现发现
其中任一项变成必要条件，必须停止在架构评审。

## 3. 正文模式的直接拖放

### 3.1 单一交互

正文移动和跨栏插入统一采用：拖选 → 从已选高亮区域开始拖动 → 实时显示合法放置光标 →
松手提交。现行产品不再提供“移动所选”“加入初稿”“移动到这里”或等价的按钮式写流程。

- 从左侧初稿高亮区拖到左侧合法位置是 `move`；词语、句子、段落或连续多个段落均可跨章节
  移动。来源范围从原位置删除并在目标边界插入。
- 从右侧原稿高亮区拖到左侧合法位置是 `insert/copy`；只复制 core 已解析的当前单一素材
  exact refs，原始 Transcript 和右侧选区内容不变。
- 正文中的标题只显示章节边界，不是整章拖动手柄。上述任意粒度的正文范围都可从一个章节
  移入另一章节，但普通文字选区不能包含标题项；拖动标题本身或以标题暗示整章移动均非法，
  整章排序只在章节模式完成。
- narration 只能按第 3.2 节的完整块规则选择和移动，目标也只能位于该块之前或之后。

### 3.2 Word 式 pointer 与 narration 对象状态机

状态机只依据 pointer 起点、既有选择范围、同步位移是否越过冻结的像素阈值和 pointerup；不得使用
长按、timeout、轮询或 `requestAnimationFrame` 竞速猜测意图。

1. pointerdown 位于当前高亮选区内时，先进入 `selection-press`，保留旧选区。
2. 在 pointerup 前累计位移超过拖动阈值，进入 `drag-existing-selection`，拖动旧选区。
3. 未超过阈值即 pointerup，取消旧选区并在点击位置放置普通 caret；零写入。
4. pointerdown 位于当前选区外时，浏览器选择行为立即建立/扩展新选区，不拖动旧选区。
5. 只有已由 core selection resolver 解析且未降级待确认的非空高亮范围可以开始业务拖动。
6. 建立新选区必须先读取并冻结浏览器 Selection endpoints，再清除旧业务选区或 patch DOM；不得让
   第一次跨过旧选区的拖动被旧状态消费。

#### 3.2.1 业务拖动取得原生 Selection 的唯一手势所有权

- pointerdown 位于 core 已解析高亮内时，先保留普通点击语义；阈值以内不得在 pointerdown 就永久
  禁用文字选择。只有同一 pointer sequence 的同步位移越过冻结阈值并进入业务拖动时，UI 才取得
  该手势的唯一所有权。
- 进入业务拖动的瞬间必须阻止后续浏览器原生文字选择默认行为，清除本次手势产生的临时 native
  `Selection`，保留 core 已解析的旧高亮，并通过 `setPointerCapture` 或经真实浏览器证明等价的
  机制确保后续 pointermove/pointerup 继续由业务拖动接收。拖动期间不得出现从起点延伸的新原生
  蓝色选区，也不得让 native Selection 与业务插入线并存竞争。
- pointerdown 位于高亮外时仍必须允许浏览器正常拖选；不得全局设置永久 `user-select: none`。
  高亮内未越阈值的 pointerup 取消旧高亮并放置普通 caret，零写入。
- 工程自动化兼容回归覆盖 Chromium/WebKit 的真实 pointer/Selection/Range 差异；项目发起人只需在自己
  使用的一个受支持 macOS 稳定浏览器中，用鼠标或触控板完成一次真实 UX 验收。自动 DOM 回归不能代替
  项目发起人的 UX 门；本机没有 WebKit 时只记录未验证，不安装或下载浏览器。

narration 不从文字选择吸附起拖。单个 narration block 只从卡片宽拖动区或左侧拖动带建立对象拖动，
命中区明显大于三个点图标；textarea、按钮和正文文字区域继续用于编辑、选择、复制。跨越两个
narration block、跨 narration 边界，或 narration 与普通正文混选均非法。拖放时 `block_id`、文字、
`status`、占位语义和完整 `recorded_refs` 原样整体移动，不得拆分或重新绑定。

拖动阈值是 UI 常量并在 pointerdown 时冻结；它不是计时器，也不因网络响应变化。鼠标和触控板
使用同一状态机。

### 3.3 本地拖动与 drop 提交

- pointermove 只做当前 DOM/布局快照上的本地 hit-test、合法性判断、边缘自动滚动和插入线更新；
  不向 Review server 发请求，不轮询解析结果。
- 拖动开始时冻结 source candidate artifact ref/hash、checkpoint ref、resolved selection 或 exact
  source refs、operation ID 和当前 Snapshot identity。目标插入线必须随命中实时更新；非法位置
  不显示可提交光标。
- `Esc`、拖回原位、未越过阈值、落在选区内部、落在标题/拆分 narration/栏外或无合法光标处
  均取消且零写入。
- drop 只发送一次第 3.4 节的私有 Review draft-edit 输入。client 只提交命中的 display point，
  不提交 `boundary_id`、document index 或其他伪装成 core 已解析 target boundary 的字段。
- stale basis、非法 target、move 的 target 位于选区内部、重复但尚未成功的 operation ID、失效
  selection 或 publish/checkpoint 失败均零写入。相同 operation ID 已成功后的重试只读回同一
  result，不产生第二次写入；不同 basis 不得复用结果。
- 同一 Review session 同时最多一个 drop 写请求在途；未得到成功、明确失败或重新同步当前
  checkpoint 前不得提交下一写操作。延迟/乱序响应只可与 operation ID、basis 和 drag generation
  完全匹配时改变 UI；旧响应本身不触发写入，晚到 server 的旧请求因 checkpoint CAS 零写入。
- client 只接受当前 drag generation 的响应；旧成功、旧错误和旧插入线结果不得覆盖新状态。
- 每次成功只有一个 operation record、一个 immutable child 和一次 checkpoint 前移；不得把
  HTTP 尝试次数、controller trace 或临时 DOM 状态算作业务操作。

### 3.4 一次 drop 的私有 HTTP 输入

`POST /api/workflow/draft-edit` 的 schema 2 drop 输入是私有、拒绝未知字段的闭合 envelope：

```json
{
  "schema_version": 2,
  "operation_id": "dwop_7_0123456789abcdef0123456789abcdef",
  "expected_checkpoint_ref": {
    "generation": 7,
    "checkpoint_hash": "<64 lowercase hex>"
  },
  "expected_current_candidate_ref": {
    "artifact_id": "draft_current",
    "schema_version": 2,
    "content_hash": "<64 lowercase hex>"
  },
  "operation": "move_selection",
  "accept_degraded": false,
  "source": {
    "kind": "resolved_selection",
    "resolution_hash": "<64 lowercase hex>",
    "surface": "draft",
    "selection_kind": "source_excerpt",
    "display_range": {
      "anchor": {
        "paragraph_id": "draft_paragraph_1",
        "block_id": "block_quote_1",
        "utf16_offset": 0
      },
      "focus": {
        "paragraph_id": "draft_paragraph_1",
        "block_id": "block_quote_1",
        "utf16_offset": 4
      }
    },
    "block_ids": ["block_quote_1"],
    "refs": [
      {
        "source_id": "src_a",
        "transcript_version_id": "tr_a",
        "segment_id": "seg_1",
        "start_ticks": 0,
        "end_ticks": 120000
      }
    ],
    "canonical_text": "来源原话",
    "degraded": false
  },
  "target": {
    "paragraph_id": "draft_paragraph_2",
    "block_id": "block_quote_2",
    "utf16_offset": 6
  }
}
```

narration 对象移动复用同一外层 envelope、`operation:"move_selection"` 和既有 `target`，但 `source`
恰好是以下另一闭集 variant：

```json
{"kind": "narration_block", "block_id": "narration_1", "text": "完整解说文字", "status": "draft", "recorded_refs": []}
```

- `operation` 只允许 `move_selection | insert_source_refs`。`move_selection` 的 `source` 必须是
  闭集二选一：普通 `source_excerpt` 使用 core selection resolver 返回的 `resolved_selection` 原样身份，
  shape 恰好包含
  `kind/resolution_hash/surface:"draft"/selection_kind:"source_excerpt"/display_range/block_ids/refs/canonical_text/degraded`，
  其中 display anchor/focus 各自恰好使用 `paragraph_id/block_id/utf16_offset`，block IDs 与 refs 均
  非空、有序，`canonical_text` 非空且由 refs 派生，`degraded` 是 boolean；narration 使用上示
  `narration_block`，恰好包含 `kind/block_id/text/status/recorded_refs`，`status` 只允许
  `draft|approved|recorded`；不得包含 `resolution_hash`、`display_range`、`surface`、`selection_kind` 或
  `degraded`。两种 shape 不得混用字段。top-level
  `accept_degraded` 是 boolean；只有 source_excerpt 的 `degraded:true` 且 UI 已完成既有明确披露/接受
  时才可为 true，source_excerpt 未降级或 narration 时必须为 false。client 不得重新计算或删改已冻结字段。
- `insert_source_refs` 的 `source` 必须是
  `{"kind":"exact_source_refs","source_id","transcript_version_id","refs","canonical_text"}`；
  refs 非空、有序、来自同一 active source/transcript binding，ticks 和 canonical text 由 core
  selection resolver 回显并在 drop 时重验。不得传裸文本、跨素材 refs 或 client 派生时码。
- `target` 恰好是当前 Snapshot 暴露的 `paragraph_id + block_id + utf16_offset`。offset 是从零开始、
  位于该 paragraph DOM text 的 UTF-16 code unit 边界；`block_id` 必须是该显示点所属或紧邻的
  持久 block identity。两个 block 之间的边界固定使用右侧 block ID；只有文稿末尾使用左侧最后
  block ID。空章节以其 heading block 的专用空 paragraph、`utf16_offset: 0` 表示；
  文稿末尾以最后一个可见 paragraph/block 的 UTF-16 长度表示，不使用 `null`、`-1` 或“追加”哨兵。
- client 在 narration pointerdown 越过冻结阈值时，从当前已验证 Snapshot 冻结完整 `narration_block`
  对象且不发请求；pointermove 仍纯本地。drop 继续携带当前 candidate/hash、checkpoint、operation ID
  和既有 target，不增加 HTTP 或 Snapshot 字段。
- server 必须在同一 Project lock 内依次验证 operation ID/checkpoint/current candidate CAS，按
  candidate hash 重新加载 Snapshot。source_excerpt 仍重验 resolved selection，insert 仍重验 exact
  source refs；narration 则按唯一 `block_id` 读取当前 block，逐字段核对 `text/status/recorded_refs`，并
  验证 target 不在本 block 内，不调用 narration 文字 selection resolver。只有 target boundary 合法后
  才可 prepare/publish 一个 child、写一个 operation record 并前移 checkpoint；block 缺失、字段变化、
  目标非法、stale 或任一验证失败全部零写入。
- pointermove 不请求 server。client 的本地 hit-test 只决定插入线候选，不能伪造 resolved target、
  将 UTF-16 offset 当作 exact ticks，或绕过 server 的 exact refs/CAS。该私有 schema 不新增公共
  workflow action、CLI/MCP tool、Proposal/Decision schema 或第二套章节状态。

## 4. 章节模式

章节模式是目录/大纲视图，以左侧章节列表替换正文，不显示本章全文，也不建设第二套正文编辑器。
每章一条紧凑章节行，只显示拖动把手或明确可拖动区域、章节标题、内容数量或“空章”和一个“…”
菜单；单击章节行回到正文模式并滚动到该章节。整章排序只使用直接拖动整行/把手，实时显示明确
的章节间插入线，松手只提交一次，并移动标题及章内全部内容。菜单只提供重命名、并入上一章（第一
章不显示）和“删除本章及内容”；删除沿用可撤销语义，不增加危险确认。并入上一章只删除当前标题，
完整保留两章内容和顺序，不是删除内容。章节页不提供拆分；正文模式先放置准确可见 caret，再用
“从这里开始新章节”输入标题，复用 `section_split`，非法边界和 narration 内部 fail closed。

`POST /api/workflow/draft-edit` 复用第 3.4 节的
`schema_version + operation_id + expected_checkpoint_ref + expected_current_candidate_ref`，其余键
恰好为 `operation` 和 `payload`；`operation` 只允许以下五项，`payload` 使用对应闭合 shape。
拒绝未知 operation/字段，不新增公共 workflow action：

- `section_reorder`：payload 恰好为
  `{"heading_block_id":"section_a","before_heading_block_id":"section_b"}`。
  移动 `heading_block_id` 及其后直到下一 heading 之前的完整范围；空章节只移动 heading。
  `before_heading_block_id` 必须是另一现存 heading；值为 `null` 唯一表示移到文稿末尾。目标为
  自身、自己章节内部或不能改变顺序时零写入失败。
- `section_rename`：payload 恰好为
  `{"heading_block_id":"section_a","title":"新标题"}`。只改变该 heading 的 trim 后合法
  title，保留 heading ID 和全部内容；同名合法且不触发合并。
- `section_split`：payload 恰好为
  `{"heading_block_id":"section_a","target":{"paragraph_id":"draft_paragraph_2","block_id":"block_quote_2","utf16_offset":6},"title":"新章节"}`。
  target 使用第 3.4 节同一表示并必须解析为该章节内 source block 之间/内部的合法 exact boundary；
  narration 内部非法。split 可位于 heading 后的空 paragraph 或章节末尾，从而显式产生空的前半/
  后半章节。新 heading block ID 只能由 core 在 prepare 中创建，client 不得提交或预留该 ID。
- `section_merge`：payload 恰好为
  `{"heading_block_id":"section_a","direction":"previous","adjacent_heading_block_id":"section_prev"}`
  或 `direction:"next"`。core 必须验证所给 heading 确为该方向紧邻章节；与上一章合并删除当前
  heading，与下一章合并删除 adjacent heading。除此以外全部 source/narration 内容与顺序原样保留。
- `section_delete`：payload 恰好为 `{"heading_block_id":"section_a"}`。删除该 heading 及其后
  到下一 heading 或文稿末尾的全部内容；空章节只删除 heading。非空与空章节都沿用现有可撤销
  删除语义，不新增危险确认门。

章节边界的单一表示始终是 heading block identity：一个 heading 到下一 heading/文稿末尾之间为
该章；相邻 heading 表示空章节，最后一个 heading 后无内容表示末尾空章节。不得另存 section index、
title map 或可漂移 section membership。

章节操作和正文操作使用同一 immutable child、operation record、checkpoint CAS、幂等和失败零写入
规则。切换模式时若存在未完成拖动或编辑，先取消本地暂态；不得并发提交另一模式的操作。

## 5. Agent 大纲重组的必须保留内容（后续独立子阶段）

本阶段不得与第一批正文拖放/章节结构代码混在一个提交。后续 Agent 重组必须满足：

- 始终以请求发起时的当前 Content Draft 为 parent，不从早期初稿、大纲 snapshot 或 Transcript
  重新生成。
- 用户可为本次修订指定一个或多个必须保留的 source excerpt 或 narration block。
- source excerpt 按 exact refs、canonical text 和 ticks 全部一致验证；narration 按完整 block
  identity、文字、状态和 `recorded_refs` 全部一致验证。任何缺失或变化都 fail closed 并请求确认。
- 除用户授权的 mutable scope 外，所有项逐字段、逐身份且相对顺序不变；preserve set 是 mutable
  scope 内的额外保留约束，不扩大可修改范围。
- preserve set 只属于本次 revision request/receipt，不持久化进 Content Draft，不建立长期锁定
  系统、第二套稿本或新的批准门。
- Agent 返回待用户审阅的一个新 immutable child，不自动确认初稿、不生成或采用粗剪。

该子阶段应复用 `content_draft_revise_scoped` 的当前-parent/完整 candidate 校验思想；其精确公开
input 变更须另行 docs-only 评审，不与本次 schema 2 标题类型升级顺带实现。

## 6. 公开 schema 和工具影响

独立 `section_title` 类型必须进入公开 Content Draft 输入/输出，因此实现阶段允许且只允许一次最小
tool schema `26 → 27` 升级：

- CLI：`content-draft-create`、`content-draft-revise-scoped`、`content-draft-read` 的 Content Draft
  block input/output，既有 `content-draft-confirm`/`content-draft-propose` 对 schema 2 的接受与回显，
  以及 `workflow-action submit_draft` 的闭合 draft input；confirm/propose 参数不变，不新增命令/action。
- MCP：对应的 `content_draft_create`、`content_draft_revise_scoped`、`content_draft_read`、
  `content_draft_confirm`、`content_draft_propose` 和 `workflow_action` tool schema；confirm/propose
  request shape 不变，媒体、组件、Job 和 Render 工具不变。
- Review server：既有 draft Snapshot/selection/caret/draft-edit 私有 HTTP envelope 可为 schema 2
  item 和章节闭集 operation 做最小版本化；它不是新增 Agent 公共工具。
- tool-contract mirror：`docs/agent-tool-contract.md` 以及
  `agent-skill/skills/{create-roughcut,render-roughcut,revise-roughcut,roughcut-basics,roughcut}/references/tool-contract.md`
  六份 byte-identical canonical/mirror 文件必须在同一个后续纵向实现提交同步。

此次 docs-only 提交不修改上述工具契约文件。后续升级不得顺带改变九个 workflow action 名称、
其他 action input、Proposal/Decision/Render、Timed Transcript、runtime binding、health、M2.7 或
Windows guard。Agent preserve-set 的公开 input 不包含在 schema 27 标题类型升级中。

### 6.1 所有 schema 2 child-producing 路径

从 core `0.2.0` 起，下列既有路径凡成功创建 Content Draft child，都必须写 schema 2 且只写独立
heading 项；schema 1 parent 先按第 2.2 节投影并在同一原子操作中产生 schema 2 child：

- Review 正文 move/insert/delete、narration 编辑和五个章节 operation；
- `content_draft_create(parent_draft_id=...)`，以及无 parent 的新稿创建；
- `content_draft_revise_scoped`；
- `workflow_action` 的既有 `submit_draft`；
- `content_draft_confirm` 创建的 confirmed child。

`content_draft_read` 必须按请求 artifact 自身的 schema 原样读取：schema 1 不假装 schema 2、不写
投影，schema 2 返回独立 heading。`content_draft_propose` 必须同时接受合法 schema 1/2 confirmed
draft；对 schema 2 逐项验证但忽略 `section_title` 进行 Proposal 编译，绝不为 heading 生成
Proposal item、clip、时长或占位。上述公共面统一包含在唯一 tool schema `26 → 27` 升级中；不得
再为 confirm/propose 或 Review 私有 operation 增加第二次 tool schema bump。

## 7. 左侧正文显示标点校订契约

本节只冻结左侧 `source_excerpt` 的人工显示标点校订，不修改生产代码、测试、静态资源或六份
canonical/mirror（`docs/agent-tool-contract.md` 与五份 Skill mirror）。既有正文拖放、原稿插入、章节、history、
标题层级和菜单关闭的人工 UX 分项已通过；用户随后新增并批准本范围，所以该历史节点的 M3.4 A 总门当时暂不关闭。
该 dated contract 不再维护 M3.4 总项、M3.4 B、M2.7 总项或 Phase 4 的当前状态；这些状态以 current-main 权威规划为准。

### 9.1 三层文字职责与用户边界

1. 右侧原始转录稿在初稿页面继续只读。本功能不修改原始 ASR 或 Timed Transcript。人名、地名、
   专有名词和错字继续走既有 `transcript_correct → immutable Transcript child → explicit activate`
   流程；已冻结 Content Draft 不自动迁移到新 Transcript。
2. 左侧正文中的同期声字词仍由 exact refs 唯一决定。汉字、字母、数字、emoji、空格、换行、顺序、
   来源、exact refs 和 ticks 都不能通过标点入口修改。选中包含同期声字词的现有范围后执行删除，仍是
   删除媒体内容的 exact-ref 操作，不是文字改写或标点操作。
3. 左侧正文中的显示标点允许用户在合法 caret 边界显式添加、删除或替换。所有 `source_excerpt`
   都可直接校订，不要求先 move/insert；移动后的原位置、新位置和原稿插入连接处也可校订。系统不得
   自动补充、删除、携带或猜测标点。

用户是否修正缺失、多余或不理想标点，均不得改变 refs、入点、出点、时长、视频顺序或粗剪生成，
也不得让 Proposal/Render 因语法或标点不理想而失败。校订只影响正文、稿件和字幕的显示文字。

### 9.2 Content Draft schema 2 的最小表示

schema 2 的 `source_excerpt` 加性增加可选 `display_text`：

```json
{
  "block_id": "block_x",
  "kind": "source_excerpt",
  "refs": [
    {
      "source_id": "src_a",
      "transcript_version_id": "tr_a",
      "segment_id": "seg_1",
      "start_ticks": 0,
      "end_ticks": 120000
    }
  ],
  "canonical_text": "我们先检查设备然后开始放映",
  "display_text": "我们先检查设备，然后开始放映。"
}
```

- `canonical_text` 继续由 exact refs 唯一派生且不可修改，代表同期声与媒体真实性。
- `display_text` 是同一个 source block 的人工阅读/字幕显示；它不是第二套正文、第二份段落状态、
  旁路 map 或 `editorial_punctuation` block。Proposal/Render 的媒体只依赖 refs，显示文字可优先使用
  `display_text`。
- `display_text` 只在与 `canonical_text` 不同时出现；相同时必须省略。只在实际校订的最小必要
  source block 保存，不复制媒体、Transcript 或时码。
- `display_text` 是公开可读、由 core 保存和传递的人工校订结果，不是 Agent 可自由填写的出稿字段。
  只有本节私有 `punctuation_edit` 可以首次引入或改变保留内容的人工标点序列；既有 move/delete/section
  和公共语义出稿只能随真实 source 内容确定性保留、切分、移动或删除该结果，不能替保留的原话新写
  标点。新加入的 source 内容从 canonical 显示开始。
- schema 1 artifact 原样只读，不原地增加字段。schema 1 的第一次成功标点操作与其他
  child-producing 操作一样，在同一原子 prepare/publish 中生成 schema 2 child；失败零写入。

合法性按 Unicode code point 严格定义：从 `canonical_text` 和 `display_text` 中分别移除 Unicode
General Category `P*` 的所有 code points 后，剩余序列必须逐个完全一致。不得做大小写转换、Unicode
normalization、空白折叠或文字纠错；空格、换行、汉字、字母、数字和 emoji 均不能改变。每一个本次
发生变化的最大连续标点 run 在操作后的最终结果最多 8 个 `P*` code points；相邻而未被替换的标点也
计入这 8 个，不能只检查 payload `replacement`。空字符串表示删除该位置的显示
标点，但受第 9.4 节“媒体 source block 不得变成空或仅空白”约束。canonical 中未被本次编辑触及的
既有超长标点 run 不受追溯限制；一旦对该 run 的任一部分插入、删除或替换，最终整个 run 必须满足
上限。server 以 core 运行时的 Unicode General Category 判定为权威，client 只作同规则的即时提示。

可选字段把两种职责保留在同一 `source_excerpt`：canonical 证明“说了什么并对应哪段媒体”，display
证明“用户希望怎样阅读和显示”。存储开销只发生在实际校订的 block，不为每个标点创建 artifact，
也不复制未校订段落或媒体数据。

### 9.3 display/canonical/exact refs 的确定性映射

- server 依据当前可见文字（有 `display_text` 时使用它，否则使用 `canonical_text`）解析用户看到的
  caret 和选区；媒体选区仍只能解析成 exact refs。
- 标点 caret 只要求位于 source block 当前显示文字的合法 Unicode code point 边界，不要求该位置同时
  是 trusted fine-unit/tick 边界；本操作不改变 refs，本来就不需要为标点伪造媒体时码。UTF-16 offset
  不得落在 surrogate pair 中间。只有实现选择把一个 source block 真正拆成两个持久 blocks 时，拆分点
  才必须另行满足既有 trusted boundary pair；无法安全拆分时保留一个 block 的完整 `display_text` 即可。
- 高亮范围明确包含的显示标点随所选内容 move；未包含的标点留在原位置。系统不得扩大选区来携带
  相邻标点，也不得因 move/insert 后原位置或新位置标点不自然而拒绝媒体 child。
- 仅选择标点不形成媒体 selection，不生成零时长 clip、媒体占位或 recorded ref；它只能成为本节
  的标点范围。删除包含任何同期声字词的现有选区继续走 exact-ref 删除，`punctuation_edit` 不得
  冒充该操作。
- 在 source block 内部校订时，core 可在真实可信边界拆分该 block，并把 `display_text` 写到最小
  必要 block；拆分只能复用已有 exact refs/trusted boundary pair，不能伪造 ticks。
- 标点插入点或替换范围必须确定性归属一个 `source_excerpt` block。两个相邻 source blocks 的同一视觉
  边界使用右侧 block；paragraph/文稿末尾使用左侧最后一个 source block；只有一侧是 source block 时
  使用该侧；两侧都不是 source block 时非法。一次替换不得跨越两个 block identity，页面也不得把跨
  block 的相邻标点合成一个可写范围。
- block 后续被 move/delete/section 操作拆分或重建时，core 必须以不变的非标点 code point 序列为
  锚，确定性切分和保留 `display_text`。显示标点不得丢失、重复或串到相邻原话。
- move/delete 后未被选中的显示标点优先留在原显示边界，并确定性归属仍相邻的 source block。若操作会
  留下没有任何相邻 source block 可承载的纯标点孤片，必须在写入前零写入拒绝并提示用户把该标点一并
  选中或先删除；不得自动删除、强行随选区移动、创建无 refs block 或复制媒体 refs。
- 若 `display_text`、`canonical_text` 与 exact refs 不能唯一映射，必须 fail closed；不得自动丢弃
  校订、回退显示 canonical、猜相邻边界或重绑目标。

### 9.4 唯一私有操作 `punctuation_edit`

现有私有 `POST /api/workflow/draft-edit` 增加且仅增加一个 `punctuation_edit` operation。它继续使用
现有 schema version、`operation_id`、`expected_checkpoint_ref` 和
`expected_current_candidate_ref` envelope；operation payload 必须恰好为：

```json
{
  "paragraph_id": "draft_paragraph_x",
  "block_id": "block_x",
  "start_utf16_offset": 12,
  "end_utf16_offset": 13,
  "replacement": "，"
}
```

- `start == end` 是插入；`start < end` 是替换或删除；`replacement == ""` 是删除，但结果不得使一个
  携带媒体 refs 的 source block 变成空字符串或仅空白。canonical/display 只有标点或空白的异常块可
  替换为其他非空标点，但不能通过本入口删成不可见；用户若不要其媒体，仍走既有正文删除。
- 被替换范围必须只含当前显示中的 Unicode `P*` 标点。`replacement` 必须只含 `P*`，且最多 8 个
  Unicode code points；offset 为当前 paragraph 可见文字的 UTF-16 code-unit 半开区间，start/end 都
  必须落在完整 Unicode code point 边界，不得切开 surrogate pair；应用 replacement 后与左右相邻
  标点合成的最终最大连续 run 也必须不超过 8 个。
- server 在同一 Project lock 内重读 current candidate，按当前 display text 重验 paragraph/block/
  range，重验 canonical 与 display 的非标点序列、exact refs、operation ID 与 checkpoint/candidate
  CAS，再 prepare/publish。
- 成功恰好一个 operation、一个 immutable Content Draft child 和一次 checkpoint 前移。no-op、非法
  字符、超限、stale、旧 basis、范围包含非标点文字、边界歧义或任一发布失败均零写入。
- 沿用既有 `invalid_workflow_change` 与 stale 边界；不新增公共 workflow action、公共 error、CLI/MCP
  command 或第二套写入口。

### 9.5 受约束的正文输入会话

正文原话 DOM 继续只读，不把正文整体改为 `contenteditable`。caret 位于合法 source body 边界时，
直接键入标点只进入本地“待保存标点”状态。一个本地会话绑定开始时的 candidate/checkpoint、block、
display range 和 caret 位置；连续输入最多累计 8 个标点，不按每个按键创建 child，也不使用 timeout
自动保存。

会话只以以下显式事件结束：点击其他位置、方向键离开或开始其他编辑前，先将本次完整变化提交为
一个 `punctuation_edit` 请求并等待成功/明确失败；成功后才执行后续动作，失败则保留待保存状态或按
stale 规则转只读，不能与页面切换竞速。`Esc` 取消尚未提交的本地变化并恢复当前持久显示。页面关闭
或刷新前仍有待保存变化时，必须触发明确离开提醒，不得静默丢失。

输入规则：

- 汉字、字母、数字、emoji、空格或换行不改变 DOM、不发送请求，并在 caret 附近提示：
  “正文原话不能直接改写；识别错误请校正原稿。”
- 活动选区含任何非标点正文时，输入标点不得覆盖该选区。
- `Backspace`/`Delete` 只可删除同一 source block 中本地会话刚输入的标点，或 caret 前/后、活动选区内
  已持久显示的 `P*` 标点；命中汉字、字母、数字、emoji、空格、换行或跨 block 范围时拒绝且零请求。
  不得依赖 `keydown` 猜最终字符；粘贴、IME/composition 提交和 `beforeinput` 的最终文本都执行同一
  Unicode 分类、范围和容量校验，正文 DOM 始终由受控状态渲染。
- 粘贴只要含任一非标点 code point，整次粘贴拒绝，不部分过滤。
- 当前 caret/选区左右会与结果相连的既有标点也占用容量；第 9 个逐键输入的标点不加入、不发送请求，
  保留本地会话中最终 run 内已接受的最多 8 个并提示：
  “一个位置最多输入 8 个标点；多余内容未加入。”一次纯标点粘贴只有在整段都放得进当前剩余容量时
  才全部加入；只要会使会话超过 8 个，整次新粘贴全部拒绝，不截取可容纳的前缀，粘贴前已接受的本地
  内容保持不变，也不产生请求或持久写入，并提示“一个位置最多输入 8 个标点；本次粘贴未加入。”
  会话以后正常结束时仍只提交已接受内容的一次请求。
- server 必须重复执行全部字符、范围、长度、basis 和 non-punctuation equality 校验，不能信任前端。

### 9.6 Proposal、字幕、媒体和空章节

- 不修改 Proposal/Decision schema。Proposal clip 的 refs、ticks、时长和媒体身份继续只来自 canonical
  exact refs。Proposal、粗剪字幕和稿件显示优先使用已验证 `display_text`，缺失时使用
  `canonical_text`。
- 新 Proposal/Render 校验不得再要求显示文字与 canonical 逐字符相等；必须要求移除 Unicode `P*`
  后的 code points 逐个完全一致，且差异只来自已验证的 `display_text` 标点校订。标点本身不生成
  clip、时长、媒体占位或 recorded ref。
- 一个 source block 含多个 exact refs 时，core 必须按各 ref 的 canonical 子串与既有固定连接边界，
  将 block `display_text` 确定性投影到对应 Proposal clips；不能把整段 display 重复到每个 clip，也不能
  把边界标点随机分给相邻 clip。固定连接边界前的标点归左 ref、之后的标点归右 ref；不能唯一投影时
  fail closed。该显示切分不改变任何 clip refs/ticks/时长。
- 标点缺失、错误或用户未修正不是确认初稿、生成 Proposal 或生成粗剪媒体的阻断条件；同一 refs 的
  校订前后版本必须产生完全相同的 clips、source ranges、ticks 和总时长。
- 空章节仍只指一个 `section_title` 后直到下一标题或文末没有 `source_excerpt`/`narration`。`display_text`
  或标点不是独立章节内容；无相邻 source block 的空章禁止创建悬空标点，不用虚假正文或标点填充。

### 9.7 性能、存储和后续工具同步

- 只在被修改的最小 source block 增加 `display_text`；不复制媒体、Transcript、时码或整篇正文；不为
  每个标点创建独立 artifact。一次连续本地编辑只创建一个正常 Content Draft child。
- candidate snapshot 仍只重建一次并原子替换；不增加轮询、全文 Transcript 重载或按键级网络请求。
- 实现阶段必须增加“长稿数百处标点校订”的存储量、载入、滚动、Undo/Redo、Proposal 编译和刷新
  一致性测试；性能结论不得只来自两三段短 fixture。
- 本字段属于 Content Draft schema 2 的公开影响。core `0.2.0` 尚未发布，后续实现必须在现有 tool
  schema 27 内一次性同步 CLI/MCP 与六份 canonical/mirror（`docs/agent-tool-contract.md` 与五份
  Skill mirror）；不得升级
  到 28。本 docs-only 评审不修改这些文件。
- 公开路径的方向必须写清：`content_draft_read` 返回可选字段；`content_draft_confirm` 原样保留；
  `content_draft_propose` 只消费 core 已验证字段。无 parent 的 `content_draft_create`/首次
  `workflow_action submit_draft` 输入必须省略字段；有 parent 的 create、`content_draft_revise_scoped`
  和 submit 输入也不得由 Agent 指定变化，core 只为未变或可按第 9.3 节唯一映射的 inherited source
  内容保留字段；被语义操作删除的 source 内容连同其 display 一起删除，新加入内容不带 display，映射
  不能唯一确定时 fail closed。不得因此形成第二个标点写入口。
- 不新增公共 workflow action/error，不修改 Proposal/Decision schema，不新增第二套章节或正文状态。

### 9.8 明确排除的独立后续项

- **段落换行**：原则上不应影响时码，但会影响段落身份、人物署名、搜索、拖放目标和 section split，
  必须作为独立产品项评审；首轮 Enter 不创建换行。
- **Transcript 校正入口优化**：既有 `transcript_correct` 能处理人名、地名、专名和错字。未来可在
  右栏增加“校正这段原稿”，仍创建 immutable Transcript child 并显式激活；已冻结 Content Draft
  不自动迁移。本轮不实现 UI。
- **move/insert 结果状态**：r104 当时记录为后续目标；该历史排除曾被第 9.10 节取代，而被动
  `result_highlight` 又被第 9.11 节的现行 ActiveSelection 契约 supersede。
- 自动补标点、AI 断句、自由文字编辑、空格编辑、换行编辑和原稿直接改写均不在本轮。

### 9.9 必须通过的契约向量

1. 无标点原话添加逗号和句号，refs/ticks 不变。
2. 删除 canonical 中已有标点，右侧原稿保留，左侧 `display_text` 删除；只有标点的 source block 不得
   被删成空白。
3. 逗号替换为句号。
4. move 后原位置遗留标点不修改，媒体仍成功。
5. move 后分别修改原位置和新位置标点。
6. 未经过 move/insert 的正文直接校订。
7. 汉字、数字、emoji、空格、换行输入及删除拒绝且零写入；Backspace/Delete 只删除标点，UTF-16
   surrogate pair 不可切开，IME/paste 走同一校验。
8. 相邻既有标点计入容量；第 9 个逐键标点拒绝但保留最终 run 内已接受的最多 8 个；含非标点或超过
   剩余容量的整次粘贴全部拒绝、不截断且零写入。
9. stale/CAS 失败零写入。
10. Undo/Redo 恢复 `display_text`，refs/ticks 始终不变。
11. Proposal 显示校订标点，媒体 clips 与未校订版本完全相同；多 ref block 的显示按固定边界唯一
    切分，不整段重复、不随机归属。
12. schema 1 首次标点编辑原子生成 schema 2 child。
13. 空章节禁止悬空标点。
14. source block 后续拆分、移动、删除后 `display_text` 不丢失、不重复、不串到相邻原话；相邻 block
    边界使用固定归属，纯标点孤片零写入拒绝；公共 Agent 出稿不能改写保留 source 的人工标点。

若本节标点向量需要改变非标点字符、exact refs/ticks、公共 workflow action/error、Proposal/Decision
schema、tool schema 28、自由 `contenteditable`、第二套正文状态，或无法唯一确定 move/split 后
`display_text` 归属，立即停止并返回架构评审；换行、Transcript 校正 UI 不得混入标点实现。

### 9.10 M3.4 A move/insert 成功结果高亮契约（历史，已被 9.11 supersede）

本节曾 supersede r104/9.8 中“move/insert 结果高亮是后续目标、本轮不做”的历史表述；其中
`result_highlight` 不是 active selection、Delete disabled、不能直接再次拖动及必须重新建立选区等
语义现已被 9.11 supersede。本节仅保留历史 wire/publish 取证。它当时冻结唯一的
`result_highlight` 实现契约：一次性、纯 UI 的结果标记，不是 `active selection`，不新增持久状态，
不允许客户端猜测新位置，也不建设通用 decoration/history 框架或第二套 Selection。

#### 9.10.1 成功结果与私有 response

- `move_selection` 成功后，只高亮新 child 的新 Snapshot 中被移动内容的新位置；
  `insert_source_refs` 成功后，只高亮新 child 中本次新插入的那一次 occurrence。成功后退出
  `active selection`，不保留可删除、可拖动或可试听的活动选区。Delete 按钮保持 disabled，
  从 `result_highlight` 上 pointerdown 不直接开始业务拖动；用户必须重新建立普通选区才能继续。
  result highlight 必须明显，但与 active selection 使用不同的视觉/ARIA 语义，不声明 `selected`。
- 只有 schema-2 `move_selection` 和 `insert_source_refs` 的 Review 私有 HTTP 201 response 增加该字段，
  且仅限本次请求新近完成持久化、`DraftWorkspaceState.readback=false` 的成功。response 结构为：

```json
{
  "result_highlight": {
    "kind": "draft_edit_result",
    "candidate_id": "<本次成功生成的新 child ID>",
    "operation": "move_selection | insert_source_refs",
    "display_range": {
      "anchor": {
        "paragraph_id": "<新 snapshot paragraph identity>",
        "character_offset": 0,
        "utf16_offset": 0
      },
      "focus": {
        "paragraph_id": "<新 snapshot paragraph identity>",
        "character_offset": 0,
        "utf16_offset": 0
      }
    }
  }
}
```

  `candidate_id` 必须等于同一 response 的 `draft_editor.candidate.candidate_id`；`display_range` 的
  两端必须各自引用同一 response 新 Snapshot 中对应的 paragraph identity，且是结果在新 child 中实际可见的
  范围，方向固定为文稿正序。`character_offset` 与 `utf16_offset` 必须同时正确，二者都必须位于完整
  Unicode code point 边界，不能切开 surrogate pair。delete、`punctuation_edit`、任何章节操作、
  narration、undo/redo 和失败 response 不返回 `result_highlight`。该字段是 Review 私有 response，
  不进入公共 CLI/MCP/tool schema、Content Draft、Proposal 或 Decision。
- 相同 operation/input 的幂等回读仍返回 HTTP 201、既有 child 和对应 Snapshot，但
  `DraftWorkspaceState.readback=true` 时必须省略 `result_highlight`。readback 不表示一次新的可见放置，
  client 不得搜索、重算、恢复或猜测结果高亮。

#### 9.10.2 唯一位置真相与原子性

- 唯一位置真相是 core 在 prepare/build child 时与 prepared child 同时产生的轻量 `placement result`。
  其中的 `placement trace`（放置轨迹）只标识本次实际移动/插入的 occurrence，并且只在本次请求内存中
  使用；placement result/trace 都不写入 Content Draft、Project JSON、checkpoint、新 artifact 或回放记录。
- 在任何 immutable child 发布或 checkpoint 持久化之前，core/application commit boundary 必须把 prepared
  child 送入与正式 Draft Editor 相同的确定性内存显示投影，产生 prospective Snapshot，并据此完整形成、
  验证最终 `result_highlight`：candidate identity 等于 prepared child；anchor/focus paragraph identity
  属于该投影；范围为文稿正序的单一连续可见范围；`character_offset`/`utf16_offset` 同时正确并落在完整
  Unicode code point 边界；范围精确对应本次 move/insert occurrence；人工标点归属与 prepared child 一致；
  不包含章节标题或目标前后未移动文字。该步骤复用 prepared child、placement result 和既有内存投影，
  不读取或扫描完整 Transcript。
- 任一 identity、范围、offset、Unicode 边界、occurrence 或标点/标题归属无法唯一产生或验证时，必须在
  publish 前零写入失败。若实现无法保证 prospective Snapshot 与随后 response Snapshot 使用同一投影输入
  和 paragraph identity 规则，则不得跨越 publish boundary。
- child 发布后的 Snapshot rebuild 只能用同一个已验证 placement result 做确定性 materialize，并核对
  candidate/paragraph/range 与 publish 前 prospective Snapshot 一致；它不得首次执行语义定位、首次判断
  范围是否有效或把 publish 后不一致包装成正常成功/错误路径。不得另起 locate/read。
- client 不得搜索 canonical/display 文本、比较新旧 DOM 或整份 Snapshot、只按 refs 找第一个 occurrence、
  只按 block 文本或标题定位、按旧 target offset 自行平移，或在 timeout 后重新读取/轮询。重复原话、
  同一 refs 被多次采用时，placement result 必须只命中本次新插入/移动的 occurrence。
- 一次成功仍只有一个 draft-edit POST、一个 operation、一个 immutable child、
  一次 checkpoint 前移；result_highlight 与新 Snapshot 在同一个 HTTP 201 response 返回。
- 计算必须复用本次 prepare 结果和新 Snapshot，不重新读取或扫描完整 Transcript；pointermove 继续纯
  本地 hit-test/插入线/自动滚动，零 server 请求。不得增加第二个 locate/read 请求、轮询、timeout 或
  异步竞速。`readback=true` 不产生第二个 child、operation 或 checkpoint，也不恢复 placement result。
  响应丢失继续作为不明确网络失败 fail closed：client 不显示结果高亮，重试回读也不补发；刷新后同样
  不恢复。

#### 9.10.3 UI 生命周期与可见范围

- 成功应用新 Snapshot 后立即显示 result highlight，并更新普通状态文字：move 使用“正文已移动，已高亮新位置。”，
  insert 使用“原稿内容已插入正文，已高亮新位置。”结果高亮不使用 timeout 自动消失；
  滚动、查看右侧原稿、搜索和播放都不清除，给用户足够时间辨认。
- 以下动作在开始时清除 result highlight：建立新的普通选区或 caret、开始标点编辑、新的
  move/insert/delete/章节/narration 操作、Undo/Redo、切换正文/章节模式、确认初稿、加载另一个
  candidate、stale 锁页和刷新页面。刷新后不得恢复，它不是项目历史或持久状态。失败、非法落点和
  stale 不产生结果高亮；既有失败保留/锁页规则不变。
- 结果范围必须与实际 child 内容完全一致。fine-unit 吸附后的完整实际内容全部高亮，不能退回用户
  最初较小的 raw `display_range`。用户明确选中的人工标点随内容移动并进入结果高亮；未选人工标点
  留在原位置，不进入结果高亮。
- 多段 move 保留内部段落边界，result range 从新位置第一段实际起点延伸到最后一段实际终点；目标段
  未移动的前后文字不能被高亮。跨章节 move 不高亮章节标题，除非未来另有明确章节语义；本轮标题不
  是移动内容。narration 不纳入本项。
- `display_range` 必须是一个能忠实表达结果的连续范围。若多段结果不能用单一连续范围表达，立即停止
  架构评审，不临时改成任意 `ranges` 数组。实现只允许一个轻量内部 placement result 和一个 UI
  `result-highlight state`，不持久化 result range。

#### 9.10.4 视觉优先级与验收向量

视觉优先级冻结为：

`active selection > result highlight > insertion caret > current search > other search > correspondence > playback > hover`

result highlight 不得与 active selection 使用完全相同的视觉或 ARIA 语义；它清楚可见但不声明 selected。
必须覆盖以下验收向量：

- **A 重复原话**：同一 canonical/ref occurrence 在稿中出现两次；insert 只高亮本次新 occurrence，
  不高亮既有 occurrence。
- **B fine-unit 吸附**：用户原始选择“说：”，实际 resolved 为“主持人说：“请大家”；move 后高亮
  必须覆盖实际完整范围。
- **C 人工标点**：未选首尾人工标点不随内容移动也不进入结果高亮；显式选择时随内容进入结果高亮。
- **D 段内、跨段、跨章节**：三类 move 的新位置准确；保留段落边界，不高亮章节标题和目标前后未移动文字。
- **E insert**：右侧原稿不变；左侧只高亮新插入 occurrence；right-side active selection 成功后退出。
- **F 交互**：结果高亮存在时 Delete disabled；从结果高亮 pointerdown 不直接开始业务拖动；重新拖选
  后才形成 active selection。
- **G 原子性**：每次成功只有一个 POST、operation、child、checkpoint；pointermove 零请求，无额外
  locate/read。
- **H 失败与历史**：400、409 和不明确网络失败不产生结果高亮；同 operation/input 的
  `readback=true` 返回既有 child/Snapshot 但省略结果高亮且零额外写入；Undo/Redo 清除；刷新不恢复。

### 9.11 2026-08-15 Word 式可信结果选区后续修订（现行）

本节完整 supersede 9.10/r105/r106 的被动 `result_highlight` 客户端语义，不同时兼容旧字段；9.10
只作为明确标注的历史记录保留。现行目标是把本次移动或插入的实际新位置直接装入既有唯一
ActiveSelection，使它继续具有 Word 式选中内容的全部能力。若下述可信选区不能在同一次
prepare/publish 内形成，而需要第二次 HTTP 或客户端猜测，立即停止架构评审。

#### 9.11.1 私有 `result_selection` 与可信身份

只有本次新发布、`DraftWorkspaceState.readback=false` 的 schema-2 `move_selection` 或
`insert_source_refs` Review 私有 HTTP 201 返回下列完整 wire；不得定义弱化 result range、第二套
Selection 或兼容 `result_highlight`：

```json
{"result_selection": {
    "surface": "draft", "request": {"anchor": {"paragraph_id": "draft_paragraph_new", "offset": 0, "offset_encoding": "utf16"}, "focus": {"paragraph_id": "draft_paragraph_new", "offset": 4, "offset_encoding": "utf16"}},
    "response": {
      "candidate_id": "draft_child_new", "surface": "draft",
      "resolution": {"direction": "forward", "canonical_text": "实际结果", "refs": [{"source_id": "src_a", "transcript_version_id": "tr_a", "segment_id": "seg_1", "start_ticks": 0, "end_ticks": 120000, "canonical_text": "实际结果"}], "start_caret": {"paragraph_id": "source_paragraph_1", "boundary_id": "boundary_start", "source_id": "src_a", "transcript_version_id": "tr_a", "character_offset": 12, "utf16_offset": 12, "degraded": false, "degradation_reason": null}, "end_caret": {"paragraph_id": "source_paragraph_1", "boundary_id": "boundary_end", "source_id": "src_a", "transcript_version_id": "tr_a", "character_offset": 16, "utf16_offset": 16, "degraded": false, "degradation_reason": null}, "adjusted": false, "degraded": false, "degradation_reasons": []},
      "display_range": {"anchor": {"paragraph_id": "draft_paragraph_new", "character_offset": 0, "utf16_offset": 0}, "focus": {"paragraph_id": "draft_paragraph_new", "character_offset": 4, "utf16_offset": 4}}, "resolved_display_range": {"anchor": {"paragraph_id": "draft_paragraph_new", "character_offset": 0, "utf16_offset": 0}, "focus": {"paragraph_id": "draft_paragraph_new", "character_offset": 4, "utf16_offset": 4}},
      "correspondence_groups": [{"source_id": "src_a", "source_display_name": "素材 A", "paragraph_id": "source_paragraph_1", "start_offset": 12, "end_offset": 16}], "resolution_hash": "<new candidate-bound 64 lowercase hex>"
    },
    "accepted_degraded": true
}}
```

`request` 必须由新 child 实际 resolved 结果范围产生，anchor/focus 固定为文稿正序并与
`resolved_display_range` 一致，不得带父 candidate 或原 raw 坐标。`response` 是完整既有
`DraftEditorSelectionResponse`：除上示新 candidate、draft surface、canonical text/exact refs、start/end
caret、degradation reasons、两种 display range、correspondence groups 和新 candidate 绑定的
`resolution_hash` 外，不得省略该模型已有身份。request 与两种 display range 使用 Draft paragraph/offset；
source carets 与 correspondence groups 使用 Transcript paragraph/offset，两套坐标不得混同。既有可选
narration 字段继续沿用现行模型规则，不新增字段或语义。结果 request 已是实际 resolved 范围，因此
`response.resolution.direction` 固定 `forward`、`adjusted=false`；`degraded` 如实反映结果 fine-unit
可信度，但操作成功已完成所需披露/接受，故所有成功 `result_selection.accepted_degraded` 固定为 `true`。
UI 只将 snake_case wire 机械映射为既有 camelCase ActiveSelection（包括
`accepted_degraded → acceptedDegraded`），不得重新解析、搜索或猜测。

响应 eligibility 是闭集：`readback=true`、delete、`punctuation_edit`、五项章节 operation、narration、
Undo/Redo 及任何失败 response 都省略 `result_selection`。该选区不持久化，不进入 Content Draft、
Project JSON、checkpoint、Proposal、Decision、CLI/MCP、公共 tool schema 或 Skill mirror；tool schema
保持 27，不新增公共 workflow action/error。刷新、Undo/Redo 或切换 candidate 时不得重算或恢复。

#### 9.11.2 同一次 prepare/publish 的产生顺序

core 继续使用 9.10 已冻结的 placement trace、prepared child、prospective Snapshot 与 publish 前
完整验证。prepare 必须从同一 placement result 形成上述完整 `DraftEditorSelectionResponse`；在发布
任何 immutable child/checkpoint 前验证 candidate/paragraph identity、完整实际范围、双 offset、Unicode
边界、exact refs/canonical text、人工标点归属、标题排除与新 `resolution_hash`。publish 后只用正式
Snapshot materialize 并核对相同身份，不首次 locate。不得新增第二个 locate/read、selection-resolve、
轮询、timeout、额外 Snapshot/Transcript 扫描或客户端 occurrence 搜索。

客户端成功顺序固定且不可交换：

1. 收到并完整验证包含 `result_selection` 的 response；
2. 应用新 Snapshot 和最终 DOM；
3. 将 `result_selection` 写入既有唯一 ActiveSelection，清除旧 source selection/caret；
4. 只渲染一次最终活动选区及相交 decorations；
5. 最后解除 busy/interaction lock。

任一步失败都不得显示猜测选区。一次成功仍恰好一个 POST、operation、immutable child 和 checkpoint；
pointermove 纯本地、零请求。

#### 9.11.3 Word 式 pointer 与浏览器 Selection 生命周期

- 用户建立新选区时，controller 必须先同步读取并冻结浏览器 Selection 的 anchor/focus endpoints，随后
  才能 invalidate caret、清除旧 ActiveSelection、替换文字节点、patch DOM 或重画段落。禁止在读取
  endpoints 前清除业务状态或破坏原 Range 所依赖的 DOM。
- pointerdown 位于活动选区内时，位移未越过冻结阈值是普通点击，pointerup 折叠为合法 caret 并清除
  活动选区；越过阈值才取得本 sequence 的业务拖动所有权并拖动现有完整活动选区。
- pointerdown 位于活动选区外并拖动时，本次 gesture 第一次就建立新浏览器选区；即使新范围跨过旧
  活动选区，也不得先消费或吞掉一次操作。只有冻结 endpoints 后才进入 selection resolve/pending。
- move/insert 成功后的新位置是左栏唯一 active selection，复用现有强反色与 `selected` 语义；用户可
  立即 Delete、播放或从其内部再次拖动。点击其他位置按上述规则折叠 caret。
- `canonical offset is out of bounds` 是 Selection endpoints 与最终 DOM 坐标漂移的明确回归向量，
  必须检查 capture-before-patch 顺序；不得统一映射为普通无效落点。

#### 9.11.4 搜索、定位与活动选区的视觉组合

active selection 独占强背景色。当前搜索命中与活动选区重叠时，以清晰橙色轮廓、内边框或等价
第二通道叠加，并继续显示当前命中序号；其他搜索命中使用更浅的非遮盖标记。禁止用第二层不透明背景
覆盖强反色。左侧产品文案只称“活动选区/选中内容”；右侧原稿 correspondence 称“定位标记”，查找称
“搜索命中”，避免把不同状态都称作高亮。实现可以继续复用既有分段 renderer，但不得引入通用
decoration/history 框架。

#### 9.11.5 narration 是对象拖动，不是文字吸附

narration 的文字、状态、占位和 `recorded_refs` 是一个不可拆对象。撤回此前“选中解说任意文字便
吸附完整块”作为唯一拖动入口；该入口在当前 DOM 不可达且会与 textarea/正文
文字编辑、选择和复制竞争。现行入口是解说卡片的宽拖动区域或左侧拖动带，命中范围必须明显大于
三个点图标，并有明确 grab/grabbing 反馈。textarea、按钮及正文文字区域仍分别用于编辑、动作、文字
选择或复制，pointerdown 不触发块拖动。解说与正文混选、跨两个解说块仍非法；narration move 不返回
`result_selection`，不新增第二套章节或 narration 持久状态。

#### 9.11.6 响应式 shell 与验收 runner

Review 初稿模式外层 shell 必须填满可用 viewport，不得用 `width: min(1600px, 100%)` 或等价规则限制
整个应用。每个阅读栏内部可以限制舒适行宽，但双栏编辑区本身应利用可用宽高，不能整体停留在居中的
小框。浏览器兼容回归是实现验证，不向普通用户暴露“Chrome 模式/Safari 模式”。后续人工验收使用
正常安装的 Chrome，由普通用户模式启动；需要证据时只附加 CDP。`--no-sandbox` 明确定责为验收
runner 参数，不是产品能力，禁止以该参数启动后续人工验收浏览器。

#### 9.11.7 display punctuation HTTP 400 的独立门

只读验收事件证明：在首次含结果状态的成功 201 之前，普通 `move_selection` 已被 HTTP 400
`invalid_workflow_change` 拒绝，message 为
`draft editor selection/caret cannot uniquely retain display punctuation`。因此该问题先于结果选区出现，
不能归因于 `result_selection`。本 docs-only 修订不猜测完整真实 request body，也不宣称已解决。

生产实现前必须先取得一个真实、可重复失败 payload，完整覆盖 operation、candidate、raw/resolved
range、exact refs、block IDs、target 与 response，再评审唯一归属规则。继续冻结：人工标点不改变
refs/ticks/media/时长/粗剪；真实歧义 fail closed；不得放宽 exact refs、吞掉标点、自动带走未选标点或
伪造时码追绿。该类 `invalid_workflow_change` 的可见提示必须具体为：

> 当前选区两侧的人工标点无法唯一保留；请把相关标点一并选中，或先调整标点后再移动。

不得统一显示“这个落点没有产生有效调整”。若真实普通操作在现行 `display_text` 模型下无法形成唯一
规则，而需要修改 schema、增加第二套标点状态或继续堆条件分支，立即停止架构评审。

#### 9.11.8 保留边界与停止条件

继续冻结 Content Draft schema 2 和独立 `section_title`、schema 1 原样只读与首次 child 升级、exact
refs/ticks/媒体身份/原素材只读、Project lock/CAS/prepare/publish/不可变 child、placement trace 与
发布前后确定性核对、一次成功一个 POST/operation/child/checkpoint、标点媒体安全、五项章节私有
operation 及 tool schema 27。该历史契约不进入已取消的 former M3.4 B，也不维护 M3.4、M2.7 总项或 Phase 4 的当前 checkbox 状态。

下一步允许在独立任务中修改必要生产代码及私有 Review response/envelope。只有实现需要公共
action/error、tool schema 28、持久第二套状态、额外 HTTP/轮询/timeout、客户端 occurrence 定位，
或放宽 exact refs/CAS/标点媒体安全时，才停止并返回架构评审。
