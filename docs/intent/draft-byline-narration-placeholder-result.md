# 初稿人物标注与未配音解说占位施工结果

状态：施工记录。不是独立验收结论；最终验收由主代理独立复核。

主代理已完成独立复核及合并前逐项核对，结论见
`draft-byline-narration-placeholder-acceptance.md`（含 A4 边界证据补充和 A8 帧率扩展）。
本文前部冻结设计和分轮命令保留原记录；最终 DOM 位置、兼容序列化及发布方式以
§2.3 修正记录和当前代码为准。目标软件实机导入仍未验证。

基线：`2863355`；分支 `evaluate-roughcut-narration-placeholder`。
计划文档：`docs/intent/draft-byline-narration-placeholder-plan.md`（保留其验收标准）。

## 1. 选定的最小 schema/字段、兼容性与生命周期策略

第 1.1/1.2 节是实施前的冻结设计；第 1.3 节记录施工过程中对冻结设计的两处细化，
均为字段级向后兼容的最小扩展。

### 1.1 WP1 初稿人物标注

- 只读视图（draft editor snapshot）扩展，不写入正文、canonical_text、exact refs、时间码：
  - 段落级新增 `bylines: [{start_offset, end_offset, text}]`（正文 UTF-16 偏移，纯展示，
    不参与选区/复制/搜索/caret/拖放/时码解析）。
  - `source_runs[]` 新增 `byline_key: string | null` 与 `byline_text: string | null`
    （映射人物为 `person:<person_id>`；未映射有 local speaker 时为
    `speaker:<source_id>:<transcript_version_id>:<local_speaker_id>`；无 local speaker 时
    保守回退 `segment:<source_id>:<transcript_version_id>:<segment_id>`）。
  - 未映射 local speaker 的显示名是 `未命名发言人 · <source display name> · <local_speaker_id>`；
    key 含 source，不同素材的 `spk_0` 永不合并。
- 显示规则（服务端按快照一次算出）：文档首个来源 run 显示；人物切换显示；显示中的章节
  标题（含附着在来源段落上的标题）之后、解说段之后重新显示；连续同一身份不重复。混合
  人物段落按 source run 边界产生多个标注。
- 前端只按 `paragraph.bylines` 渲染独立 `.draft-byline` 节点，位于正文容器
  `[data-editor-text]` 之外，不带 `data-editor-text`/`data-text-fragment`；`paragraphSignature`
  纳入 `bylines`/`narration_duration_seconds`/`narration_estimated_seconds` 与 run 的
  byline 字段，保证局部 patch、undo/redo 与接缝重渲染立即正确。
- 旧快照（无新字段）前端不渲染草稿标注，行为与旧版一致；不迁移旧 artifact。

### 1.2 WP2 未配音解说

- `NarrationBlock` 新增可选 `estimated_duration_seconds: int | None`（正整数秒，人工覆盖；
  null/缺省 = 自动估算）。schema 1 序列化不变；schema 2 序列化新增该字段，解析兼容缺省。
  保存、读取、Agent/CLI/MCP 提交、child 归一化、复制/移动、撤销/重做均保留该字段。
- 估算（唯一实现，后端）：Unicode 码点中非空白、非 Unicode P* 标点的字符计数 N；
  秒数 = `max(1, ceil(N * 60 / 200))`（整数运算）。前端只显示后端给出的
  `narration_estimated_seconds`，不重复计算。
- 有效时长（唯一实现，后端）：recorded → `recorded_refs` 实际时长（不帧对齐）；
  未录音 → 秒数转项目整帧向上取整覆盖：`ceil(seconds*TICKS/tpf) * tpf`，整数 ticks。
  草稿时长摘要、Proposal/Decision、RenderPlan、多机位投影、NLE 全部复用同一函数。
- 唯一剪辑序列：在既有 Proposal/Decision 的 `clips` 有序集合中新增带类型变体
  `NarrationPlaceholderClip`：
  `{kind: "narration_placeholder", clip_id, narration_block_id, media_version, media_id,
  media_frames, source_in_ticks, source_out_ticks, reason, display_text}`。
  - 旧 `EditClip` 序列化不变（保持旧 artifact 内容哈希可复读）；新变体只在真实存在时出现。
  - 公开 Proposal 构造器（Agent/CLI/MCP 输入）拒绝 `narration_placeholder`；
    占位只能由 core 从已确认初稿的已 approved 解说编译产生。
  - `narration_block_id` + `display_text` + `media_id` 使旧 Decision 自解释，不依赖当前可变稿。
- 生成媒体：
  - 项目内 `generated/narration-placeholder/<media_id>.mp4` + 同名 `.json` 记录；
    `media_id = "phm_" + sha256(v1|width|height|frame_rate|audio_sample_rate|video_frames)[:24]`。
  - 内容：H.264/yuv420p + AAC 双声道数字静音，黑底，固定文字"解说待配音"。
  - 中文渲染来源显式解析：FFmpeg `drawtext`（libfreetype）能力探测 + 固定顺序字体路径列表
    （macOS PingFang/STHeiti/Songti，Linux Noto CJK/文泉驿，Windows 微软雅黑）；
    探测或字体缺失时 fail closed（typed error），不新增运行时依赖，不假设 drawtext/字体存在。
    解析出的字体路径记入媒体记录，作为渲染来源证据。
  - 生命周期：`ensure` 幂等——记录+文件存在且 fingerprint 匹配则零 FFmpeg；缺失才生成；
    记录与文件不一致、内容哈希不符则 fail closed，不覆盖旧 Decision 引用的媒体。
    原子发布（temp + fsync + replace）。纯读取/编译信任检查不调用 ensure。
  - 生成素材不进入 `project.sources`、ASR、对轨、人物映射、真实转写绑定。
- RenderPlan：`RenderPlan`（单源 schema 1）新增可选 `generated_sources` 字段（文件解析兼容
  缺省），与 `MultiSourceRenderPlan` 的 `sources` 并列参与 clip 解析、fingerprint 校验与
  FFmpeg 输入；复用现有 trim/concat/schedule。`timeline.py` 无需改动（占位是普通 span）。
- Preview：review snapshot 的 `sources` 增加占位媒体条目（kind=video，display_name=解说待配音，
  media_url=/media/<media_id>），媒体端点仅对快照授权 ID 提供该记录解析出的项目内文件。
- 多机位：并行投影新增 `narration_placeholder` slot 分类与 `placeholder_ref`，不再伪装
  mapped/missing；`placeholder_ticks` 进入覆盖统计（mapped+missing+uncertain+conflict+
  placeholder=total）；主副机位同区间显示同一占位视频（其自带静音音轨）；Alignment 内容
  与对轨算法不改。
- NLE：占位解析为真实媒体 clip，主副轨同区间；主轨无 gap。导出时把生成媒体作为 sidecar
  复制到 `<destination stem>_placeholder_media/`（已存在且哈希一致则复用，不一致 fail closed），
  XML locator 指向交付后的 companion 媒体；receipt 新增可选 `placeholder_media` 绑定
  （media_id/media_version/content_sha256/locator_hash/duration_ticks），request hash 与回读
  校验覆盖它。无解说或已 recorded 的项目不生成、不附带任何新增文件。

### 1.3 施工中的两处最小细化（兼容性说明）

1. `NarrationPlaceholderClip` 增加 `media_frames`（正整数，生成参数）。原因：编译产出的
   Proposal 必须能被后续消费者幂等重建同一媒体（缺失时重新生成、不能猜 duration→frames
   的哈希原像）。旧 artifact 不含该变体，因此序列化向后兼容不受影响；`EditClip` 不变。
2. 同一份编译产物共享一份媒体：编译时取该初稿全部占位所需时长的最大值 + 2 帧余量得到
   `media_frames`/`media_id`，所有占位 clip 从同一媒体裁切（`source_in=0`，`source_out=各自
   有效时长`）。因此"一份媒体覆盖最长占位段，各段重复引用"成立；不同初稿/更长文本会得到
   新的 media_id，旧 Decision 引用的媒体不被原地延长或覆盖。
   ensure 调用点相应为：review snapshot、RenderPlan 创建、多机位 parallel prepare、NLE
   export 准备（编译本身是纯信任检查，不触发 FFmpeg；媒体缺失或校验失败时 fail closed）。
3. 章节标题重置规则按服务端"显示中的 section title"判断（标题可能附着在来源段落上），
   附着标题的段落同样重新显示人物。

## 2. 实施与验证记录

### 2.1 变更范围（diff 摘要）

- 领域：`domain/edit.py`（占位 clip 变体与解析）、`domain/timeline.py`（占位 span 使用
  media_id）、`domain/content_draft.py`（估算/有效时长）、`domain/render.py`
  （generated_sources）、`domain/multicam_parallel.py`（slot/camera 闭集扩展）、
  `domain/nle_handoff.py`（NLE receipt `placeholder_media` 绑定，向后兼容解析）、
  `domain/narration_placeholder.py`（新增：媒体身份/记录/字体候选）、
  `domain/workflow_actions.py`（submit_draft 可选 estimated_duration_seconds）。
- 应用：`content_drafts.py`（编译共享占位媒体、时长摘要）、`draft_editor.py`（bylines）、
  `draft_workspaces.py`/`preview.py`/`proposals.py`/`renders.py`/`workflow_review.py`/
  `workflows.py`（导出一致性）、`edits.py`（类型放宽）、`readable_transcripts.py`
  （占位 overlay/脚本导出）、`narration_media.py`（新增：ensure/生成/校验）、
  `multicam_parallel.py`（占位投影与输入）、`nle_handoff.py`（companion 交付与 receipt）。
- Review server：`/api/workflow/draft-narration` 可选时长、占位媒体 `/media/<phm_...>` 授权、
  `_snapshot_bindings_stale` 忽略生成媒体（否则采用阶段被误判 stale）。
- 前端 `review-ui`：`workflow-types.ts`/`types.ts`、`workflow.ts`（行内 byline 边界插入、
  UTF-16→codepoint 边界转换、caret 几何回退、时长编辑 UI、行签名）、
  `draft-dom-endpoint.ts`（annotation→正文边界映射）、`workflow-handoff.ts`（approved 即可
  提案）、`roughcut.ts`/`roughcut-model.ts`（占位稿件 run、当前素材显示"解说 / 待配音"）、
  `style.css`（行内标注 user-select:none、时长控件样式）。
- schema/工具：`mcp.py` narration block 可选字段与 propose 描述。
- 文档：`docs/agent-tool-contract.md`（及其 5 份 agent-skill 镜像逐字节同步）、
  finite-workflow / draft-editor / draft-workspace-checkpoint / multicam-alignment /
  nle-handoff 架构契约受影响段落。
- 生产 assets：`review-ui` build 同步到 `core/src/roughcut/review/static/`
  （index-vhIG7YV4.js 118,171B；index-ATHO8ei0.css 41,449B，均在既有预算内，
  未再次上调）。
- 刻意调整（非验收标准变更）：`core/tests/packaging/test_review_assets.py` 的单 chunk 体积
  预算 115,000 → 120,000、总 static 预算 170,000 → 175,000（新增 byline/时长/占位 UI 后
  基线 114,970 的 115,000 预算必然溢出；仍保持"framework-free 且有限"的约束）。

### 2.2 实际执行的命令与结果

Python（仓库根，`PYTHONPATH=core/src:. core/.venv/bin/python`）：

```text
core/tests/domain        323 passed   （+6：占位时长/分数帧率、旧 artifact 兼容 golden）
core/tests/application   1025 passed, 14 skipped   （+12：发布安全回归等）
core/tests/adapters      867 passed, 9 skipped    （+1：旧 receipt canonical 回读）
core/tests/review        116 passed
core/tests/integration   53 passed   （含新增验收短工程，~19s）
core/tests/e2e           4 passed
```

顶层其它测试（`core/tests` 除上述目录）与基线对比：现存的 bootstrap/component-installation/
code-build-identity/catalog 类失败在干净基线的临时检出（无 .git、无网络/组件缓存）中同样
失败或走不同分支；工作区脏树本身会触发 `bootstrap requires a clean Git source checkout`。
对比清单保存在 `.acceptance/baseline-failures.txt` 与 `.acceptance/current-failures-2.txt`
（未纳入提交）。前端 assets 的 `test_review_assets.py` 3 项通过，`python -m build` 相关 2 项
因 venv 缺 setuptools（环境）报错，与基线一致。

前端（`review-ui`）：

```text
./node_modules/.bin/tsc --noEmit                                 通过
./node_modules/.bin/vitest run                                   32 files / 195 tests 通过
./node_modules/.bin/vitest --config vite.playwright.config.ts --run  2 files / 42 tests 通过（真实 Chromium）
./node_modules/.bin/vite build                                   同步生产 static assets
```

### 2.3 首轮验收缺陷修正（R1–R5，2026-10-16）

主代理首轮独立验收结论与逐项复现见 `docs/intent/draft-byline-narration-placeholder-acceptance.md`。
本轮按 R1–R5 修正，以下是代码位置与可复跑的回归证据。

**R1 混合人物标注位置（WP1）**

- 代码：`review-ui/src/workflow.ts`
  - `insertInlineBylines()` 把每条 byline 插入到其 source run 起点对应的
    `[data-text-fragment]` 之前（正文容器内、UTF-16 起点精确对齐）；无法匹配时（标点
    预览重建等）按原顺序回退为段落前标注。
  - `renderTextParagraph()` 将服务端 UTF-16 run 起点转换为 `partitionText` 使用的
    code point 边界（乙 0-1、甲😀 1-3），修复此前的 emoji 越界导致的 run 合并。
  - `endpointFromPoint()` 在 `caretPositionFromPoint` 落到不可选标注时回退到几何最近
    正文边界。
- 代码：`review-ui/src/draft-dom-endpoint.ts::endpointForAnnotation()`：点到标注（或其文本）
  解析为它所引导的 run 起点 / 前一个 run 终点，绝不产生被标注文字长度偏移的 offset。
- 代码：`review-ui/src/style.css`：`.draft-paragraph-text > .draft-byline`
  `user-select: none`，标注不进入文字选区/复制；标注节点无 `data-text-fragment`/
  `data-editor-text`。
- 回归（真实 Chromium）：`workflow-real-dom.test.ts` byline 用例现在断言
  DOM 子节点顺序 `byline→fragment:0→byline→fragment:1→byline→fragment:4`、emoji UTF-16
  边界、run 起点 caret 请求 offset=1、点标注仍解析为正文边界、拖选选区恰为“乙甲😀”
  且无标注进入选区、拖放移动的 `resolved_selection` 区间为 (0,4) 且目标段落正确、
  undo 后行内顺序立即恢复；截图输出
  `.acceptance/byline-dom/mixed-bylines.png`。
- 回归（fake DOM）：`draft-dom-endpoint.test.ts` 新增标注映射用例（标注节点/标注文本/
  编辑器容器边界）。
- 约束保持：段落身份、正文偏移、exact refs、canonical text、搜索/caret/时码语义不变；
  未新增编辑状态，未拆分持久段落。

**R2 旧 artifact canonical/兼容（WP2）**

- 代码：`domain/content_draft.py`（`NarrationBlock.to_dict` 仅在非 None 时写
  `estimated_duration_seconds`）、`domain/render.py`（两种 RenderPlan 仅在非空时写
  `generated_sources`）、`domain/nle_handoff.py`（receipt 仅在非空时写
  `placeholder_media`）、`application/nle_handoff.py`（`_request_projection` 仅在存在占位
  绑定时加键）、`domain/workflow_actions.py`（submit_draft 的解析投影省略缺省/Null
  `estimated_duration_seconds`）。
- 代码：`domain/multicam_parallel.py`：`_closed(optional=...)` 允许旧 summary/manifest
  camera/slot 缺少 `placeholder_ticks`/`placeholder_ref`（读取时 `.get` 默认 0/None，不回写、
  不改哈希）；`application/multicam_parallel.py` 的 `_slot`/camera plan 仅在占位存在时写新键，
  保证无占位 basis 与旧版逐字节一致。
- 回归：
  - `test_multicam_parallel.py`：用 pre-feature 形状的 basis 断言
    `plan_basis_hash == fd3d93e2bbe0bd95e898f9dc7135f36d05235d4965de790c95ec0eb7d4abd5d8`、
    `prepare_id == mpr_bcc59e2cdfe9125802085041a433deec`（值取自 git archive HEAD 的旧代码）；
    旧 manifest（无 `placeholder_ticks`）通过 `validate_manifest`；两种 slot/camera 形状都
    通过闭集校验。
  - `test_content_draft_schema2.py`：旧 schema2 draft dict 往返逐字节相等（含显式覆盖值）。
  - `test_render_plan.py`：旧 RenderPlan/MultiSourceRenderPlan dict 往返逐字节相等。
  - `test_nle_export_store.py`：旧 receipt JSON（无 `placeholder_media`）经
    `NleExportStore.read` 通过 canonical 校验并原样回读。
  - `test_nle_handoff.py`：无占位项目的 request projection 不含 `placeholder_media`，
    `nle_export_request_hash` 与旧投影一致，receipt `placeholder_media == ()`。
  - `test_workflow_actions_schema2.py`：缺省/Null 时长不进入解析投影，显式值保留。

**R3 删除占位后的历史回读（WP2）**

- 代码：`application/edits.py::_restorable_clip_summary()` 对占位输出显式
  `kind: "narration_placeholder"` + `narration_block_id/media_version/media_id/media_frames/
  source_in_ticks/source_out_ticks/from_edit_version_id`，不造 transcript/segment 引用；
  普通素材保持旧字段形状。`_validate_placeholder_clip_ranges()` 在
  `_validate_changed_clips`/`_validate_changed_proposal_clips` 中拒绝越过
  `media_frames × ticks_per_frame` 的 trim。
- 回归：验收小样新增 `placeholder_history` 检查：`change_edit` 删除占位后
  `read_edit_history(root)` 返回显式摘要（无 `source_id/segment_id`）；越界 trim 报
  `exceeds its generated media range`、边界内 trim 成功；review server
  `/api/roughcut-change` 删除后 `candidate_history.restorable_clips` 带
  `kind/media_id`，再经同一入口 restore 成功。

**R4 生成媒体/companion 的安全发布与失败清理（WP2）**

- 代码：`application/narration_media.py`
  - 媒体 staging 改为 `tempfile.mkstemp` 唯一名 + `publish_workflow_candidate`
    （no-replace 原子发布、fsync、父目录非 symlink 校验），不再用可预测 PID 名 + `os.replace`。
  - `_adopt_orphan_media()`：媒体已存在但 record 缺失（发布中断窗口）时，确定性重生成并
    比对 content hash；一致才补写 record（保留原 inode 与指纹），不一致 fail closed 且
    preserve。
  - `_generate_narration_placeholder_media()` 的 FileExists 竞争分支不再使用 losing staging
    的 mtime：从已验证的最终发布文件重新 probe/fingerprint（`_canonical_media_for_published_file`），
    并在写 record 前断言 `media.fingerprint == fingerprint_file(final)`，否则 fail closed。
  - `_write_new_record(final_path=...)`：并发写者已创建 record 时做有界等待读取（≤0.5s），
    仅当参数/content hash 一致**且**记录 fingerprint 等于实际发布文件时复用，否则 fail closed；
    `_adopt_orphan_media()` 返回的就是该 canonical record。
  - `_reconcile_failed_media_publication()`：record 失败时只删除本 action 发布的
    inode+content hash 完全一致的媒体；被替换/外部文件保留并报
    `cleanup refused a changed publication`。
  - `_require_directory()`/`_validate_regular_owned_file()` 拒绝 symlink 目录/非普通文件。
- 代码：`application/nle_handoff.py`
  - companion 目录为 symlink/非目录 → `nle_export_destination_conflict`。
  - 新文件用唯一 staging + `publish_workflow_candidate`（no-replace）；并发胜者文件校验
    哈希后复用但不记为 owned；既有文件要求 single-link regular + hash 一致（symlink/
    hardlink 拒绝）。
  - `_OwnedCompanionFile(dev, ino, content_sha256)`；`_remove_owned_companion()` 只删除
    inode 与内容仍一致的 action-owned 文件，替换后的文件保留，空目录仅在本 action 创建时
    移除。
- 回归：`core/tests/application/test_placeholder_publishing.py`（12 项）：
  no-replace 幂等 + inode 不变、orphan 自愈与篡改 orphan fail closed、record 失败只清理
  本次发布、被替换的发布保留并报错、并发 ensure 只产生一份媒体（barrier 注入）、
  symlink 占位目录 fail closed、companion symlink/hardlink/不同内容拒绝、复用不产生
  ownership、失败注入只清理本次创建文件并保留既有复用文件、inode 不匹配的清理不删除、
  并发 companion 发布复用同一文件；其余 NLE/媒体用例不回归。

**R5 类型与 lint 收尾**

- 修复：union 收窄改为真实 `isinstance` 分支（`proposals.py`、`edits.py`）、
  `EditClipLike` 类型放宽覆盖 history/restoration/agent revision context/render plan/
  review server、`_SlotInput` 显式 tick 类型（去掉对 `object` 的 `int()`）、
  `nle_handoff` staging 变量可空标注；`ruff --select F401,I001 --fix` 清理新增未用导入与
  导入顺序，手工修复 ISC004/F841/RET501/PLR0402 等新增项。
- 命令与结果：
  ```text
  PYTHONPATH=core/src:. core/.venv/bin/python -m mypy core/src/roughcut --no-incremental
  → Found 2 errors（均为基线 mcp.py:3521 / cli.py:172 TextIO.reconfigure，见 .acceptance/mypy-baseline.txt）

  PYTHONPATH=core/src:. core/.venv/bin/python -m ruff check core/src/roughcut     core/tests/application/test_placeholder_publishing.py     core/tests/integration/test_narration_placeholder_acceptance.py
  → Found 3 errors（均为基线 domain/nle_handoff.py 171/346/388 的 B018×2、RUF034，
    见 .acceptance/ruff-baseline.txt；未新增、未顺手改动）
  ```
- 未使用 Any/ignore 批量遮盖；未修改基线无关问题。

**预算**：JS 118,171B（原 114,970B，+3.2KB；预算 120,000B 内，未再次上调），CSS 41,449B，
总 static < 175,000B 预算。

### 2.3.1 第二轮独立复核收尾（2026-10-16）

主代理第二轮复核结论见 acceptance 文档 §第二轮独立复核：R1–R3、R5 已通过；R4 仍有一个
确定性并发缺口（A 先发布视频、B 输掉发布但先写 record、A 写 record 输掉；B 的 record 曾
保存 losing staging 的 mtime）。本轮最小收尾：

- 修复（`application/narration_media.py`）：
  1. FileExists 竞争分支改用 `_canonical_media_for_published_file()`，从最终已发布文件
     重新 probe/fingerprint 后再写 record；
  2. 写 record 前强制 `media.fingerprint == fingerprint_file(final_path)`；
  3. `_write_new_record(final_path=...)` 只在“同 generation + content hash”且“记录
     fingerprint == 实际文件”时采纳并发记录，否则 fail closed；
  4. `_adopt_orphan_media()` 返回 `_write_new_record` 的采用结果。
- 固定顺序回归：`test_placeholder_publishing.py::test_deterministic_race_losing_staging_never_writes_the_record`
  （线程名 + Barrier/Event 固定 A publish→B record→A record 的顺序），断言两个返回值与
  持久记录的 fingerprint/content hash 都等于实际文件、第三次 ensure 零重建（inode 不变、
  目录无残留）。
- 主代理独立脚本复跑：`PYTHONPATH=core/src:. core/.venv/bin/python .acceptance/independent-race-check.py`
  → `generation_errors [] results 2`、`record_fingerprint_matches_published_file True`、
  `subsequent_ensure=passed`。
- 前端收尾：移除测试中遗留的 `DEBUG-SEL` console.log；`mixed-bylines.png` 改为在任何编辑
  交互前截取（现展示 人物乙→乙→人物甲→甲😀→人物乙→乙 的真实混合画面），undo 后画面另存
  `mixed-bylines-after-undo.png`。
- 本轮命令与结果：
  ```text
  pytest core/tests/application/test_placeholder_publishing.py     core/tests/integration/test_narration_placeholder_acceptance.py -q   → 14 passed in 22.32s
  tsc --noEmit                                                          通过
  vitest run                                                            32 files / 195 tests 通过
  vitest --config vite.playwright.config.ts --run                       2 files / 42 tests 通过
  vite build                                                            同步 static assets（JS 118,171B）
  mypy core/src/roughcut --no-incremental                               2 errors（基线）
  ruff check（变更 source + 新测试）                                      3 errors（基线）
  ```

### 2.3.2 第三轮独立复核 R6 收尾：交付小样的真实 run 入口（2026-10-16）

主代理第三轮复核发现：`test_narration_placeholder_acceptance.py` 末尾在最终主样品上执行
`change_edit/delete/restore/undo/redo/trim` 与 Review 恢复，改变了 Project revision，导致
run 的 exact multicam continuation/adoption 绑定失效；按结果文档执行
`roughcut review .acceptance/narration-placeholder-sample/project --run-id wfr_sample --json`
返回 `workflow_integrity_error`（continuation expected revision 不匹配）。

本轮仅调整验收 fixture（不改生产状态机、不回写 revision/ref、不放宽 stale/continuation）：

- 主样品流程结束（采用、渲染、多机位、两种 XML、幂等/篡改检查）后先做决策预览检查，然后
  `shutil.copytree` 出 `output/lineage-project`；重排/删除/恢复/撤销/重做/trim 越界/历史
  摘要/Review 删除-恢复全部改在该副本上执行（源素材为副本外的绝对 locator，合成媒体继续
  可用）。检查名保持 `placeholder_lineage`/`placeholder_history`，新增
  `lineage_isolation` 记录隔离事实。
- 主样品在副本操作前后断言 `revision` 与 `active_edit_version_id` 不变，随后在测试内真实
  启动文档给出的 CLI 入口（`core/.venv/bin/roughcut review <project> --run-id wfr_sample
  --json`），读取 CLI 输出的 URL、用会话 cookie 依次请求首页与 `/api/review`，要求 HTTP
  200、basis 为 decision 且包含“解说待配音”占位素材；结果记为 `final_cli_review`。
- 复跑命令与结果：
  ```text
  pytest core/tests/integration/test_narration_placeholder_acceptance.py -q → 1 passed in 18.09s
  （report.json 现为 19 项检查，新增 lineage_isolation / final_cli_review，
    并记录 review_entry 与 lineage_project 路径）
  pytest core/tests/application/test_placeholder_publishing.py     core/tests/integration/test_narration_placeholder_acceptance.py -q → 14 passed in 23.82s
  ruff check core/tests/integration/test_narration_placeholder_acceptance.py → All checks passed
  ```
- 手工复核（同一命令，脚本内起子进程 + cookie jar）：首页 200 `text/html`，
  `/api/review` 200、basis=decision、占位素材 `phm_14144c7f5dfc87ca24106733` 在列；
  `workflow_status(root, "wfr_sample")` → stage `export_review`、multicam_alignment
  `succeeded`、revision 6。产物：`.acceptance/narration-placeholder-sample/`
  （project 为正式链路 current 状态、lineage-project 为历史/trim 检查副本、handoffs/、
  report.json 19 checks）。
- 交付入口（现在真实可用）：
  ```text
  core/.venv/bin/roughcut review .acceptance/narration-placeholder-sample/project \
    --run-id wfr_sample --json
  ```

### 2.3.3 合并检查 R7：正式导出 manifest 与事务恢复（主代理收尾记录）

PR #1 首次 Windows CI 发现正式 `approve_export` 在 receipt 后清理 staging 失败：
`Render manifest fields do not match its closed schema`。原因是 `_render_manifest`
无条件写入 `generated_sources`，而 WorkflowStore 闭集未同步；正式 manifest hash
投影也遗漏生成素材。主代理以现有 W6 真实测试及独立新建含占位工程复现，暂停合并。

同模型执行会话修正四个 source 文件并补测试；旧会话先归档中断再由新会话接续，始终
只有一位施工代理。主代理最终归档冻结施工，独立读 diff、完成验证并补本节记录。

- 无占位 manifest 不写空扩展，保持旧形状和 subject hash。
- `domain/render.py::render_manifest_projection` 复用现有闭集及投影规则，供正式
  hash 生成与 WorkflowStore 清理/recovery 同时使用；非空生成媒体摘要纳入 hash。
- 未知字段/空数组/null/错误生成项形状拒绝，篡改会造成候选 hash 冲突并保留现场。
- 没有改变工作流状态机或事务顺序，没有放宽审批、来源或复制工程的 scope 校验。
- 持久回归：schema 2/3 兼容与 hash、恢复失败保留现场、真实含占位正式导出、
  清理、restart/readback/幂等；既有无占位真实导出明确断言不含新字段。

主代理真实命令与结果：

```text
PYTHONPATH=core/src:core/tests:. core/.venv/bin/python -m pytest core/tests/domain/test_render_plan.py core/tests/adapters/test_workflow_store.py core/tests/application/test_workflows.py core/tests/application/test_renders.py core/tests/windows/test_w6_render_proxy.py -q
→ 281 passed, 1 skipped in 29.95s（新增 27 项）

PYTHONPATH=core/src:core/tests:. core/.venv/bin/python .acceptance/independent-r7-formal-check.py
→ 小样重建 1 passed in 16.78s；正式导出 completed，同 receipt 幂等回读；recovery clean；1 份生成媒体、2 段引用

PYTHONPATH=core/src:. core/.venv/bin/python .acceptance/independent-r7-projection-check.py
→ schema 2/3：legacy projection unchanged、generated hash bound、unknown fields rejected

ruff check（R7 source + 测试） → All checks passed
mypy core/src/roughcut --no-incremental → 原基线 2 errors，无新增
git diff --check → 通过
```

完整独立记录在 acceptance 文档 R7 节，失败/复跑输出在 `.acceptance/independent-final/`。
修正后重新提交 PR #1 并运行 Windows CI；不得将首次 CI 失败归因于环境或漏报。

### 2.4 验收短合成工程（A1–A8 的真实证据入口）

单一命令重建小样并自动检查（默认输出 `.acceptance/narration-placeholder-sample/`，
可用 `ROUGHCUT_ACCEPTANCE_DIR` 覆盖；每次先清空该目录再重建；视频不提交）：

```text
PYTHONPATH=core/src:. core/.venv/bin/python -m pytest \
  core/tests/integration/test_narration_placeholder_acceptance.py -q
```

结果：`1 passed`（本机 18.09s，19 项检查写入 report.json；主样品为正式链路 current
状态，值变更类检查在 `output/lineage-project` 副本）。小样内容与检查覆盖：

- 25 fps、320x180、两机位（main.mp4 / aux.mp4，96s 合成彩条+提示音），两位人物映射 +
  一位未映射发言人，两个章节、两段未配音解说（自动估算 3s、人工覆盖 5s），成片 14s。
- 全链：review server 建稿 → `/api/workflow/draft-narration` 保存人工时长与回读 →
  确认 → Proposal（2 个占位 clip 共享 1 个 `phm_*` 媒体，3s/5s 同源裁切）→
  `/media/<phm_*>` Range 播放授权 → 采用 → 决策预览（总时长/跨度连续）→
  主输出 MP4（350 帧/14s，占位区间黑底白字标签、解码静音，真实素材有声）→
  真实 continuation Alignment → 多机位 prepare（placeholder_ticks=8s，覆盖统计诚实）与
  parallel render（副机位 350 帧/14s、切点一致、占位区间静音）→ FCPXML 1.14 +
  FCP7 XML 交付（4 个占位 clip 共享 1 个媒体定义、主副同区间、主轨无 gap、companion
  媒体 hash 一致）→ 同 action 幂等回读 → 篡改/缺失 companion 与项目内生成媒体 fail closed。
- WP1 证据：同一小样在确认前由 draft editor 快照断言 bylines（人物甲 · 主持人 /
  人物乙 · 老师 / 未命名发言人 · main.mp4 · spk_2），并断言段落正文不含标注。
- 产物：`.acceptance/narration-placeholder-sample/{report.json, project/, lineage-project/,
  handoffs/}`，其中 `handoffs/sample.fcpxml`、`handoffs/sample.xml`、
  `handoffs/sample_placeholder_media/` 即交付 XML 与 companion 媒体；
  `project/renders/…mp4` 为主输出，`project/renders/multicam/…/camera_aux_1.mp4` 为副机位；
  `lineage-project/` 承载重排/删除/恢复/撤销/重做/trim 历史检查，主样品的 run/continuation
  绑定不被这些检查改变。

小样预览入口（测试内与手工各实测一次：CLI 启动成功，首页 200、`/api/review` 200）：

```text
core/.venv/bin/roughcut review .acceptance/narration-placeholder-sample/project \
  --run-id wfr_sample --json
```

### 2.5 A1–A8 映射

| 编号 | 证据位置 |
| --- | --- |
| A1 | 后端 `test_draft_editor.py::test_draft_bylines_*`、`test_mixed_person_paragraph_bylines_follow_the_actual_run_switch`；小样 byline 断言 |
| A2 | `workflow-real-dom.test.ts`（真实 Chromium：行内标注精确落在 run 切换处、emoji UTF-16、caret/拖选/拖放不含标注、undo 后刷新；截图 `.acceptance/byline-dom/mixed-bylines.png`）；`draft-dom-endpoint.test.ts`；`paragraphSignature` 回归 |
| A3 | 小样全链（含解说编辑/重排/删除/恢复/撤销/重做后的身份与预览）+ `test_workflow_server.py::test_unrecorded_narration_confirms_and_proposes_with_managed_placeholder`（含采用） |
| A4 | 小样 timeline/决策跨度/媒体端点；`roughcut-real-dom.test.ts`（真实 Chromium：进入/拖动/暂停/离开占位，稿件显示"解说待配音"且无"人物未对应"） |
| A5 | 小样 FFmpeg 抽帧 signalstats（黑底白字）、volumedetect（占位静音/真实有声）、主副帧数时长一致 |
| A6 | 小样 XML 结构断言 + companion 媒体 hash + 共享媒体定义去重 + 主轨无 gap |
| A7 | 小样幂等重试、companion 缺失/篡改 fail closed、生成媒体篡改 fail closed、ensure 零 FFmpeg 复用、直接编辑链路身份保留、旧 schema canonical/hash/prepare_id 兼容（golden）、发布安全与失败清理回归 + 固定顺序并发回归（`test_placeholder_publishing.py`，含主代理 `.acceptance/independent-race-check.py`）；既有 stale/CAS 测试 |
| A8 | `test_content_draft_schema2.py::test_narration_duration_estimation_is_integer_frame_aligned_and_manual_override_wins`（整数/30000-1001 分数帧率下的一帧内向上取整）；既有真实素材分数帧率/不支持情形的原始错误保持不变 |

## 3. 完成情况与未验证项

已完成：WP1（服务端标注身份/去重/章节与解说重置、行内边界渲染、真实 DOM 回归）与 WP2
（时长、唯一剪辑序列、生成媒体、预览、主/副机位渲染、FCPXML/FCP7 XML 与 companion 交付、
receipt/幂等/篡改处理、发布安全与失败清理、旧 artifact 兼容、前端时长 UI 与占位播放、
CLI/MCP schema、契约文档同步），以及首轮验收 R1–R5 的全部修正与回归。

已知未验证 / 受环境限制：

1. **FCP 与 Premiere 实机导入**未执行（计划交由主代理核验），本记录只证明 XML 结构、
   locator、媒体 hash 与 receipt。
2. 真实 Chromium 的占位播放测试使用一次性 fixture payload 与本地 range 媒体服务；
   生产 W5 CLI Review browser smoke 由主代理补跑（1 passed），本施工记录不重复声称。
3. 小样的多机位 Alignment 走真实 continuation（audalign 运行时已装且成功）；在未安装该
   运行时的机器上，多机位/NLE 部分按既有契约 fail closed（不伪造 mapped）。
4. `core/tests` 顶层 bootstrap/component-installation/code-build-identity/catalog 失败是
   环境/脏树问题（与基线对照），未在本次修复；`python -m build` 需要 setuptools。
5. 未做：ASR、TTS、真实素材长片、媒体上传、跨平台 NLE 版本验收、旧 artifact 原地迁移。
