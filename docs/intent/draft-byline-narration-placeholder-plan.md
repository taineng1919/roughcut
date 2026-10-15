# 初稿人物标注与未配音解说占位施工文档

状态：WP1/WP2 施工及独立工程验收完成。目标软件实机导入因本机无 FCP/Premiere
待验证；其余 A1–A8 已逐项核对，详见同目录独立验收记录。2026-10-16 用户追加授权：
完成核对后提交 PR 并合并。下文保留原施工范围和验收标准。

基线：`2863355fd1ddc65fa498fcb0168718f90fb06dc2`；分支
`evaluate-roughcut-narration-placeholder`。此前已 fetch 并确认与 `origin/main` 一致。
工作区已有未跟踪的 `core/uv.lock`，属于施工前状态，保留且不提交。

## 1. 交付范围与执行分工

只保留两个工作包：WP1 初稿人物标注；WP2 未配音解说从初稿确认到预览、主副机位
渲染及两种 NLE 工程导出的完整支持。WP2 的模型、编译、播放和输出是内部实现顺序，
合并为一次完整功能交付，不新增逐段审批门。

- 执行代理：通过 Paseo 创建，OpenCode / OpenCode Go / DeepSeek V4.1 Flash，thinking=max。
- 主代理：负责工程决策、独立审查 diff、独立复跑验收和检查交付小样。
- 完成后由 Paseo 原生 notifyOnFinish 通知。禁止使用状态轮询、定时查询、heartbeat
  或额外监控循环等待代理完成。执行代理不得另行派生代理。
- 本次授权为仓库功能实现及合成测试；不使用用户真实项目、不联网做 ASR、不上传媒体、
  不部署、不推送、不合并、不发布、不改写原素材或既有项目产物。

必须遵守最小修改原则：先读现有契约和相关测试，不重构工作流、声音对轨算法、渲染
或 NLE 导出架构；不做自动 TTS、外部组稿或其他功能。每一处改动都要能对应本文件的
需求。例行实现细节由执行代理解决；若必须突破上述约束，报告主代理作技术裁决。

## 2. 先读的现有契约和已确认事实

阅读 `docs/spec.md`、`docs/architecture/draft-editor-direct-manipulation-contract.md`、
`docs/architecture/draft-workspace-checkpoint-contract.md`、
`docs/architecture/finite-workflow-contract.md`、
`docs/architecture/multicam-alignment-contract.md`、
`docs/architecture/nle-handoff-contract.md` 及对应代码/测试。

本任务明确扩展旧契约中“解说必须录音才能编译”和“导出只引用原始素材”的范围，
只为已确认解说引入真实存在的、项目管理的生成视频；其余契约保持。
新需求覆盖这些旧限制，不需要因此再向用户请求授权。实施完成后更新直接受影响的
契约，避免文档与代码互相矛盾；历史兼容性证据不得重写成新功能验收通过。

已核对的阻断点：

- `workflow.ts:visibleBylineForSurface()` 对 draft 直接返回 null。
- draft editor 允许多人物文字进入同一显示段，此时段落级 person 清空；现有测试
  `test_different_person_partial_move_roundtrips_two_paragraph_memberships` 固定了此行为。
- `content_drafts.py:prepare_content_draft_proposal()` 拒绝未录音解说。
- `workflows.py:prepare_confirmed_draft_and_proposal()` 也提前拒绝；
  `workflow.ts` 的确认动作、按钮和 `workflow-handoff.ts` 的 eligibility 同样有限制。
- 确认初稿已有 draft -> approved 的解说状态转换，继续复用。
- `NarrationBlock` 无人工时长字段，`_content_draft_duration()` 只累计 refs。
- `EditClip` 强制 source/transcript/segment identity，多源 Proposal 限定 clip 属于
  source_bindings；proposal/edit 校验需要真实 transcript/segment。
- RenderPlan 的媒体目录和真实转写绑定耦合；review preview 也由绑定构建素材目录。
- `multicam_parallel.py:_project_camera_slots()` 当前把每个 clip 送入 Alignment 投影，
  slot 的 classification/coverage 是闭集；占位不属于声音映射分类。
- NLE 校验会拒绝不属于主机位的素材，主轨禁止 gap；当前导出只发布 XML 文件。

## 3. WP1：初稿人物标注

### 行为

- 初稿来源正文首次出现人物或人物切换时显示姓名与身份；连续同一人物不重复。
- 章节起点、解说之后重新显示人物，帮助读者恢复上下文；章节标题和解说保留独立样式。
- 映射人物按 person_id 判断身份；未映射人物按 source_id + transcript_version_id +
  local_speaker_id 判断。无 local speaker 时保守使用既有 segment 身份，不猜测合并。
- 未映射人物显示稳定、可区分的临时编号，不把不同素材的 spk_0 视为同一个人。
- 混合人物段落按实际 source run 的人物切换展示；不为显示标注拆分、合并或重建原段落。

### 实现边界

优先复用 `_person_identity()`、source_runs 和现有 `.draft-byline`。必要时给只读
视图的 source_runs 增加最小展示身份字段，前后端同步类型。展示标注不写入正文 text、
canonical_text、exact refs、时间码或第二套编辑状态。

标注 DOM 不带 data-editor-text/data-text-fragment，不进入文字选区、复制文本、
搜索偏移、caret/拖放命中或文字时码解析。混合段落可按 run 边界插入独立的非正文标注，
但不要打破现有唯一正文容器和 endpoint 算法的假设。

须检查 paragraphSignature、contiguousPatch、retarget 和局部 patch 对相邻人物去重的
影响。插入、删除、移动、撤销、重做之后，接缝两侧标注应立即正确，不能依赖整页刷新。

重点文件：`application/draft_editor.py`、`workflow.ts`、`workflow-types.ts`、
`style.css` 和直接受影响的 DOM/模型代码与测试。

## 4. WP2：未配音解说完整支持（一个工作包）

### 4.1 文字确认与时长

- 未录音解说可以随初稿确认；编译占位要求解说文字已 approved。草稿确认原有原子事务、
  stale/CAS、checkpoint、receipt 和采用/导出审批边界继续保留。
- 估算口径：按 Unicode 字符计数，忽略空白和 Unicode 标点；其余字符按一个字计。
  默认秒数为 max(1, ceil(N * 60 / 200))；实现使用整数运算，前后端不各算一套。
- 在 NarrationBlock 上保存可选人工秒数覆盖值，正整数秒；null 表示自动估算。
  已有数据缺字段等同 null。文本修改时自动估算重算，显式覆盖值保留；绑定真实录音后
  使用 recorded_refs 的实际时长，人工估算不覆盖录音。
- UI 在已有解说编辑位置提供简洁的时长修改，不增加新流程、侧栏或逐段确认。
- 编译一次解析出唯一有效 duration_ticks，草稿时长摘要、Proposal/Decision、预览、
  RenderPlan、多机位投影和 NLE 均复用同一结果。
- 项目分数帧率下，请求秒数需要转换为足够覆盖该时长的整项目帧，并使用整数 ticks。
  所有路径复用该有效时长；界面可读地显示实际占位时长。不得让主副轨逐段独立舍入。
  覆盖既有受支持帧率，保留旧素材原有的不可表达帧率/区间 fail-closed 规则。
- 保存、读取、Agent 提交、child 归一化、复制/移动、撤销/重做均须保留新字段；同步
  更新 closed schema、CLI/MCP tool schema 和相关前端类型。旧 artifact 不原地迁移。

### 4.2 唯一剪辑序列与来源证明

- NarrationBlock 保留 narration 身份；未录音时 recorded_refs 始终为空。
- 在现有 Proposal/Decision 的同一有序剪辑集合中表达明确的 narration_placeholder 项，
  使用最小的带类型扩展或小型变体；不要建立另一份时间线/剪辑状态。
- 占位项必须有稳定 clip 身份、解说 block 来源、有效时长和可验证的生成媒体引用；
  transcript/segment 语义不适用于它。禁止伪造 ASR、fake transcript、fake segment、
  source binding，或仅凭 reason/ID 前缀绕过来源校验。
- 普通真实素材继续受原有 transcript、canonical display、范围和 source binding 校验。
  占位只能从 core 验证过的已确认解说产生，公开任意 clip 输入不能伪装成可信占位。
- 序列化/版本识别需兼容旧单源和多源 Proposal/Decision/RenderPlan；新旧 clip 都应在
  原有编辑、采用、回读、撤销和恢复路径中成立。明确保留解说来源，不依赖当前可变稿
  才能解释旧 Decision。
- 首先记录选定的最小字段及版本兼容策略，再实现；不借此设计通用新媒体框架。

### 4.3 项目管理的普通占位视频

- 生成普通 H.264/AAC、黑底、数字静音视频，固定文字“解说待配音”；与项目输出
  尺寸、帧率、音频采样率一致。明确中文渲染来源，不能假设所有 FFmpeg 带 drawtext
  或系统都有同一字体；优先小型、确定的固定标题卡/已有能力，不新增大型运行时依赖。
- 同版项目的一份媒体覆盖最长占位段及必要帧尾余量，各段从同一媒体裁切并重复引用。
  按生成参数/版本不可变缓存，禁止原地延长或覆盖旧 Decision 已引用的媒体。
- 使用已有 probe/fingerprint/path 安全能力校验，原子发布；媒体未生成或校验失败时
  不发布成功的粗剪/导出结果。纯读取和编译信任检查不得意外触发重复 FFmpeg 工作。
- 生成素材不进入 ASR 或声音对轨，不污染用户素材范围、人物映射或真实转写绑定。

### 4.4 预览、渲染和多机位

- review snapshot 和受权限限制的媒体目录包含占位所需媒体；播放器支持进入、拖动定位、
  暂停/续播和离开占位。展示“解说/待配音”及原文字，不显示“人物未对应”。
- 生成素材沿用媒体安全边界，不新增任意文件读取或裸绝对路径接口。
- 在已有 render plan 中解析生成媒体，继续复用现有 FFmpeg trim/concat/schedule。
  `timeline.py` 的总时长和 map_output_time 能覆盖占位。
- 主、副机位的占位都占相同输出区间并显示同一占位视频，旁白期间静音；后续原素材
  恢复原有对齐关系。只在投影消费处为占位分流，不修改 Alignment 内容或对轨算法。
- 如需扩展并行输出 slot，显式区分占位和 mapped/missing/uncertain/conflict，
  保持覆盖统计和闭集校验诚实；不得把占位伪装成声音成功匹配或对轨缺失。
- 统一采用现有全局绝对帧/采样边界策略；不能逐段累计 rounding 漂移。

### 4.5 NLE 普通素材交付

- NleHandoffTimeline 主轨继续无 gap。将占位解析为真实存在的普通媒体 clip，主副轨
  时段一致。真实素材及 Alignment 的验证不得被全局放宽。
- 优先复用现有 FCPXML 1.14 / FCP7 XML xmeml v5 writer 和时间转换；无需特殊字幕、
  generator、插件、厂商私有标记或新 exporter profile。
- 两种格式引用同一份生成素材；向导出目录交付占位 sidecar（可置于稳定命名的
  companion 媒体目录）。不复制用户原素材。使用交付后的 locator，支持工程连同
  companion 媒体一起交接及正常重新链接。
- 资产指纹/输出定位参与准备和 receipt 的校验；覆盖输出冲突、幂等重试、缺失/篡改
  媒体、XML 发布或 receipt 失败的处理。只能清理本次操作拥有的文件，不覆盖既有交付。
- 无解说及解说已 recorded 的路径不生成占位媒体、不附带新 sidecar，保持原有行为。

重点范围除用户原列文件外，还包括 `domain/content_draft.py`、`domain/edit.py`、
`domain/render.py`、`domain/workflow_actions.py`、`application/workflows.py`、
`application/proposals.py`、`application/edits.py`、`application/renders.py`、
`application/preview.py`、多机位并行投影/FFmpeg adapter、review 媒体安全边界、
前端 handoff/types/roughcut/player 及对应公开 schema。按实际依赖最小修改。

## 5. 方便、可重复的验收

默认不跑真实素材长片。复用现有 synthetic fixture 能力，新增一个可重复生成的短工程：
25 fps、短彩色画面/可识别提示音、主副两个机位、两位人物及两个章节、两个不同长度
的未配音解说（一个自动估算，一个人工覆盖），总时长约 10–20 秒。
用精确合成转写和确定的 Alignment 输入测试投影，单独保留现有对轨算法回归；
不下载/运行 ASR 模型，不把确定性 Alignment 谎称为新做声音对轨。

在 `.acceptance/` 或临时目录生成媒体、项目、预览入口和两种 XML；不提交生成视频。
提供一个清晰的命令即可重建小样，自动检查直接附在现有测试/脚本中，不建新验收系统。

| 编号 | 必须提交的验证证据 |
| --- | --- |
| A1 | 姓名/身份、同人去重、跨素材同名 speaker、未知编号、章节/解说切换、混合人物段落 |
| A2 | 真实 DOM 中标注不进入选区/复制/搜索/光标；拖放、局部刷新和 undo/redo 保持 refs 与边界 |
| A3 | 编辑解说与人工时长、保存回读、确认、Proposal、采用、预览、MP4、两种 XML 的完整流程 |
| A4 | 首尾/连续/中间占位的时长、顺序、播放切换；已录音和无解说项目回归 |
| A5 | 占位画面抽帧可见中文标签，解码音频为静音，主副输出帧数/时长和切点相同 |
| A6 | XML 指向真实交付媒体；共享资源去重，主轨无 gap，轨道时间及 A/V 关系正确 |
| A7 | 旧 schema 往返、重复生成/导出幂等、失败清理、缺失或篡改占位、现有 stale/CAS 校验 |
| A8 | 既有受支持整数与分数帧率边界测试，保持真实素材导出不支持情形的原有错误 |

测试优先使用 core/.venv 和已有 node_modules/runtime。Python 从仓库根目录执行
`PYTHONPATH=core/src:. core/.venv/bin/python -m pytest ...`，避免 fixture 对 core 包
导入失败。HTTP/浏览器测试需要本机回环端口权限；遇到 sandbox bind 限制应正确报告
或按工具权限流程执行，不把环境阻断写成产品失败或测试通过。

相关基线已通过：254 项 draft/timeline/NLE writer/multicam vector 测试，81 项
render plan/render adapter/NLE application 测试，4 项 review 解说/章节确认测试，
12 项前端 byline/endpoint/handoff 测试。现有“隐藏标注/拒绝未录音”断言需更新为
新行为；禁止通过跳过、弱化断言或删掉安全校验制造绿灯。

实施时运行受影响的 Python/前端测试、必要 lint/type 检查、真实 DOM/production smoke，
以及上述短工程的真实 FFmpeg 输出验证。已有浏览器不可用时记录，勿擅自下载大型组件。
前端 build 输出至 core 随包 static 目录，按仓库惯例同步生产 assets 并验证。

## 6. 独立验收和报告

执行代理结束前提供：改动摘要、选定的数据字段/兼容策略、实际执行命令和结果、
短工程重建命令及输出路径、已知未验证项。可以写入同目录的施工结果文档，
不把自己的报告标为“独立验收通过”。不要改写本计划的验收标准以迁就实现。

主代理收到完成通知后独立读 diff、检查是否违背约束、复跑关键流程并观看抽帧/小样。
有问题则向同一执行代理发送有界修正任务，继续等待完成通知，不轮询。

实际 FCP/Premiere 导入是单独的证据项：使用同一个短合成工程做一次轻量导入检查，
确认媒体在线、文字占位可见、时长/轨道正确。可用应用由主代理核查并操作；没有目标
软件时标为“待实机导入”，不能把 XML 结构检查、旧版本验收记录或 FFmpeg 播放
冒充本次实际导入。无需用户重新跑真实素材长片，也不在未经说明时安装软件。

最终给用户一张简短验收表，明确通过、失败、受环境阻断，附小样和复现入口。
