---
name: render-roughcut
description: Guide formal export approval for an adopted roughcut, including combined main and auxiliary delivery.
---

# Render roughcut

Use the versioned local roughcut tools. Read `references/tool-contract.md`
before rendering. Do not invoke FFmpeg or ffprobe directly, alter source media,
substitute a different edit, or silently retry stale state. Keep internal stages,
hashes, receipts, bases, refs and action names out of user-facing guidance.

## 面向用户的引导

如果当前任务包含多机位平行输出，先确认主粗剪已采用且机位授权仍有效。交付固定分三条路径：

Auxiliary Source 只通过已确认的 `multicam_setup` 和显式 exact `source_pairs` 进入平行输出；
它不进入 `source_authorizations`、不做 ASR、不要求 Transcript/readiness。若 setup/pair 缺失或
Auxiliary 被错误授权，即使 `transcribe=false` 也停止，不按文件名、index 或顺序猜绑定。

- A. “主 MP4 + 选定副机位平行 MP4”（推荐）：先完成副机位准备，在同一条中文消息中展示主 MP4 与选定副机位的完整组合摘要，并等待一次批准。
- B. “仅主 MP4”：展示主 MP4 摘要，等待一次独立批准，只执行主 MP4 正式导出。
- C. “仅生成副机位参考文件”：展示副机位准备摘要，等待一次批准，只生成副机位参考文件。

如果当前任务原本已由用户确认是多机位交付，到了导出阶段发现副机位 Source、机位授权或 alignment prerequisite
缺失，必须报告“前序多机位准备不完整”并停止多机位导出准备；不得把缺失静默解释为“没有副轨”，也不得静默把原来的多机位任务降级成 B「仅主 MP4」。
只有在用户得知缺失及影响后明确决定“这次只导出主片”，才能进入 B 路径。就副机位本身而言，
alignment 只依赖已导入的原 Source 音轨及合法机位授权，不要求副机位 Transcript；不得把副机位 Transcript
声称为 alignment prerequisite。

组合摘要的批准同时是当前主 MP4 的正式导出批准；组合路径不得再次询问主 MP4 导出批准。不要要求用户
理解或提供内部技术字段，也不得替用户决定切机、替换或混音。

采用粗剪、播放预览、保存初稿、历史授权或“继续”都不等于正式导出批准。导出前用普通
语言展示：当前片段数、总时长、分辨率、帧率、音频、输出位置、是否读取原素材和预计
执行动作。用户可以用语义等价的明确表达批准正式导出当前版本，不必机械复述唯一口令；
“继续”“可以看看”或“采用粗剪”不能算导出批准。

成功时报告 MP4、manifest 和 acceptance checks；失败时报告工具返回的错误和清理状态，
不声称产物存在。

正式导出是长任务：宿主暴露 Roughcut MCP 时，使用宿主管理的原生 MCP task/session
并保留句柄；只有入口 Skill 已判定 MCP 不可用且安装诊断给出绝对 CLI 路径时，才使用
CLI 进程/session。原生 MCP 的 schema 校验或工具调用失败不等于 MCP 不可用，必须停止并
报告，不得改走 CLI。开始前用 `⏳ 正在处理` 报告精确素材范围、预计耗时范围和磁盘估算；
运行中只报告进程存活、已耗时和当前步骤，没有可信百分比 API 时严禁伪造百分比。结束后
从版本化工具响应做最终单一 JSON readback，再用 `✅ 已完成` 或 `⚠️ 需要处理` 报告结果、
实际耗时、产物和清理状态。
正式导出的 Project-media operation ID 就是调用前已保留的 action ID。宿主临时
task/session ID 失效时，只用已持有的 operation ID 调用一次 `media_operation_status`；
不得自行 `sleep` 或轮询失效 task，不得扫描 Render、staging、receipt 猜成功，也不得
自动重试。status 固定话术为：
`pending` → `⏳ 等待开始：任务已记录，媒体处理尚未开始。`；
`running` → `⏳ 正在处理：任务仍在运行。`；
`succeeded` → `✅ 已完成：任务已成功完成。`；
`failed` → `⚠️ 需要处理：任务执行失败。`；
`interrupted` → `⚠️ 已中断：执行进程已经结束，任务没有成功终态。`

## 仅供 Agent 执行的顺序

### 多机位平行输出独立分支

仅当主 Decision 已采用、机位授权仍有效，并已从 create 流程收到 exact succeeded alignment
ref 与对应 core coverage 时进入此分支。本 Skill 不启动、重启或补跑对轨。先原样展示 core
alignment coverage，再取得用户明确选择的 `auxiliary_camera_ids`。
如果原任务已确认是多机位交付，但这里发现副机位 Source、机位授权或 alignment prerequisite 缺失，必须报告
“前序多机位准备不完整”并停止；不得落入 B 路径或把原任务改写成仅主 MP4。只有用户得知缺失后明确决定“这次只导出主片”，
才能按 B 执行。副机位 alignment 只依赖已导入的原 Source 音轨及合法机位授权，不要求副机位 Transcript。

### A. 主 MP4 + 副机位平行 MP4

#### 批准前

A-1. 确认 current Decision、exact succeeded alignment ref 和用户选择的 `auxiliary_camera_ids` 仍然有效。
A-2. 在批准前调用一次 `multicam_parallel_render_prepare`，只传 exact alignment ref 和选择，保留返回的 exact `prepare_ref`。
A-3. 读取 pure prepare 摘要，原样展示覆盖、黑画/数字静音、规格、global frame/sample quota 与 planning estimate。
A-4. 将副机位 prepare 摘要与主 MP4 exact export 摘要合并展示，主摘要包含片段数、总时长、分辨率、帧率、音频和输出位置。
A-5. 等待一次组合批准。组合摘要的批准同时是当前主 MP4 的正式导出批准；组合路径不得再次询问主 MP4 导出批准。

#### 批准后

A-6. 组合批准已经取得；这里不再产生分开的明确批准，组合批准后 Host 才预持有新的 parallel operation ID，使用返回的 exact `prepare_ref` 调用一次 `multicam_parallel_render_start`。
A-7. 使用同一 parallel operation ID 做一次 `media_operation_status` readback。
A-8. 副机位 operation 无论成功或失败都不自动重试，不再次调用副机位准备。
A-9. 副机位 operation 完成或失败后，重新读取 `workflow_status`。
A-10. 若同一 Decision/export subject 仍 current，直接调用 `workflow_action(approve_export)`，使用主 MP4 自己的 action/operation ID 和 readback，不再询问第二次批准。
A-11. 若 subject 已变化或 stale，停止并要求重新确认，不能沿用旧组合批准。
A-12. 分别报告主片和副机位的真实结果；两项都成功才报告组合完成，任一失败如实报告部分完成，不复用 operation ID、不把副轨冒充主片。

批准后不得再次进行副机位准备。

### B. 仅主 MP4

B-0. 如果原任务已确认是多机位交付，B 不是副机位前置条件缺失时的自动 fallback；只有先报告“前序多机位准备不完整”
并得到用户明确决定“这次只导出主片”后，才进入本路径。
B-1. 不执行副机位准备。
B-2. 不启动副机位输出。
B-3. 展示主 MP4 exact export 摘要。
B-4. 等待一次独立批准。
B-5. 调用 `workflow_action(approve_export)`。
B-6. 做正式导出 readback，并报告真实结果。

### C. 仅副机位参考文件

C-1. 批准前调用一次 `multicam_parallel_render_prepare`。
C-2. 展示 pure prepare 摘要，包括覆盖、黑画/数字静音、规格、临时空间和输出位置。
C-3. 等待一次只针对副机位的批准。
C-4. 批准后调用一次 `multicam_parallel_render_start`，只传 exact `prepare_ref`。
C-5. 用同一 operation ID 做一次 `media_operation_status` readback。
C-6. 只报告副机位参考文件结果，不执行主 MP4 正式导出。

### 通用正式导出安全约束

对轨可交付性、slot、quota、hash 和 identity 全部由 core 返回；本 Skill 不扫描文件、不自算 eligibility/slot/quota/hash，
也不从 staging/final/manifest 推断成功。

1. 调用 `workflow_status`，确认当前用户任务允许正式导出，并只使用 core 返回的 exact
   export subject；再调用 `project_open` 读取用户可理解的导出摘要。若没有已采用的
   current 粗剪，停止，不把历史 Decision 或 Render 当作批准。输出画面、帧率和音频设置
   从当前 Project settings 读取。
2. 调用统一 `decision_read`，验证读回的
   Decision 正是 active 版本，并从它读取 clips、时长和原素材读取范围。任一
   stale 或不一致都停止并重新 `project_open`，不得静默重试。
3. 各交付路径只执行所属小节的动作和确认门，不把另一条路径的批准或 operation 迁移过来。
   不得调用低层 `render_roughcut`、直接调用 FFmpeg、替换 Decision，或把代理当作正式输入。
4. 将成功 action 的 receipt/status 与调用前已保留的 Project-media operation ID 作为唯一
   readback identity，读取并报告 MP4、manifest 和 acceptance checks。响应丢失时，有
   operation ID 就先用 `media_operation_status` 回读 exact record；同 action ID/同 input
   仍只读取原 operation/receipt。失败时读取 closed error 后停止，不扫描 staging、不清理、
   不自动重试旧请求或新建 ID。

正式导出不会修改源媒体。只有 `workflow_action(approve_export)` 的 receipt/status 返回
成功 acceptance checks，才可称 MP4 导出完成；随后读取并报告 MP4、manifest 和 acceptance checks。

### D. 可编辑 NLE handoff

NLE handoff 是独立于 MP4 的交付动作，不把 `approve_export`、采用粗剪或普通 Decision adoption
当作授权。只有用户明确要求创建可编辑工程，并在调用前看到 route/profile、destination、当前
Adopted Decision、原始素材摘要和（多机位时）exact Alignment coverage 后，才能调用
`approve_nle_export`。

固定调用字段为：`project_path`、`run_id`、新的 `action_id`、`edit_version_id`、
`expected_revision`、`route`（`fcpxml` 或 `fcp7_xml`）、`destination` 和
`alignment_artifact_id`（单机位必须是 `null`；多机位必须是当前 exact deliverable ID）。
`fcpxml` 固定生成 `roughcut_fcpxml_1_14`，`fcp7_xml` 固定生成
`roughcut_fcp7_xml_xmeml_v5`；不传版本、编码器、lane、XML tuning 或 source locator。

调用前先用 `workflow_status`/`decision_read` 取得当前 exact subject，不能自行拼接 hash、从
roughcut/parallel MP4 反推时码，或重新运行对轨。Core 会重新验证 Decision、Project revision、
SourceAsset fingerprint/snapshot、Alignment producer/deliverable 和 destination；stale、缺失源、
错误 Alignment、已有目标或 unsafe path 必须停止。

成功只以 `approve_nle_export` 的 receipt 和 output hash 为准。writer 只序列化 Core 构建的
in-memory handoff timeline；FCPXML 的 auxiliary 是 connected lane clips，FCP7 是 ordered
video/audio tracks + reciprocal links。unmapped auxiliary 区间保持 gap/absence，不生成黑色视频、
数字静音、rendered MP4 或 native multicam object。失败不得声称工程已创建，也不得自动重试或覆盖目标。
