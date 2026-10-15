# 主代理独立验收记录

日期：2026-10-16。基线 `2863355`。

当前结论：**自动化工程验收及本机生产预览独立验收通过。R1–R6 均已关闭；
FCP/Premiere 实机导入仍未验证，本机未安装这两款软件，不能声称全部验收要求完成。**
未使用生产长素材重跑；以可重建的 14 秒合成工程验证链路。代码未提交、未推送。
施工记录不是独立验收证据。本文件由主代理维护，执行代理更新施工结果文档及修正测试。

以下保留各轮真实发现和修正过程；最终结论及当前证据见末节。

## 首轮已独立复现通过

- `git diff --check`：通过。
- `cd review-ui && ./node_modules/.bin/tsc --noEmit`：通过。
- `cd review-ui && ./node_modules/.bin/vitest run`：32 files / 194 tests 通过，
  含 42 项真实 Chromium 测试。浏览器需要沙箱外本机进程权限；首次沙箱内运行的启动
  失败未计为产品失败，正确授权后复跑成功。
- `PYTHONPATH=core/src:. core/.venv/bin/python -m pytest core/tests/integration/test_narration_placeholder_acceptance.py -q`：
  1 passed in 16.54s，独立重建 14s 合成工程、主副输出和两个 NLE 交付。
  测试需要本机回环服务权限，首次沙箱内 bind 失败后授权复跑成功。
- `PYTHONPATH=core/src:. core/.venv/bin/python -m pytest core/tests/windows/test_w5_review_browser.py -q`：
  1 passed in 1.85s，补上施工报告遗漏的生产 CLI Review + browser smoke。

以上通过不抵消以下已复现的缺陷。

## 必须修正

### R1：混合人物标注丢失位置（WP1）

`workflow.ts:renderTextParagraph()` 只取 bylines 的 text，忽略 start_offset/end_offset，
把所有姓名标签依次堆在段落正文之前。对“乙甲😀乙”的真实 DOM fixture，界面会先显示
乙、甲、乙三个标签，再显示整段文字；读者无法知道哪句是谁说，未满足人物切换处标注。
现有新增真实 DOM 测试只验证标签顺序/数量及容器外隔离，没有验证视觉/文字边界位置。

要求：标注与其对应 source run 在阅读顺序中相邻，切换位置准确，段落身份和正文偏移
不变。补跨人物/emoji、跨 run 选择/复制/拖放/undo 的真实 DOM 检查及截图。
计划中“标签在唯一正文容器外”是为了隔离语义的实现约束；若要调整 DOM，保持标签
不进入正文文字、复制内容、选区、搜索、caret/refs/时码，说明最小改动即可，不新增
编辑状态或为了显示而拆分持久段落。

### R2：旧 artifact 的 canonical bytes/hash 不兼容（WP2）

独立复现结果：

- schema 2 的旧 NarrationBlock 缺 estimated_duration_seconds，from_dict 后 to_dict
  却新增 null；旧 ContentDraft 的 canonical SHA-256 不再相同。
- 旧 RenderPlan 缺 generated_sources，roundtrip 新增空数组，canonical hash 改变。
- 旧 NLE receipt 缺 placeholder_media，虽然 from_dict 接受，但 NleExportStore.read
  会报 `receipt bytes are not canonical`。
- 无占位的 NLE request projection 也无条件增加空 placeholder_media，改变旧 request_hash，
  原 action 的幂等回读因此不兼容。
- 原 schema 1 parallel summary/manifest 的 camera/slot 不含 placeholder_ticks/
  placeholder_ref，新闭集直接拒绝旧数据：`rejected non-closed parallel camera plan`。

要求：读取旧数据后保持旧 canonical 表示、hash/ref/receipt 可验证；旧无解说/已录音
项目和历史回读不产生空字段引发的行为变化。可采用省略空值的兼容加法或必要的明确
版本区分，禁止改写旧 artifact 或全局放宽 hash/闭集校验。补真实旧 JSON/receipt/parallel
record 的回归，不只测新字段存在后的往返。

### R3：删除占位后编辑历史回读崩溃（WP2）

在独立重建的小样副本中，通过 change_edit 删除一个 NarrationPlaceholderClip，随后
read_edit_history(root) 报：

`AttributeError: 'NarrationPlaceholderClip' object has no attribute 'source_id'`

原因：application/edits.py 的 restorable_clips 摘要仍直接读取
source_id/transcript_version_id/segment_id。现有小样做了删除/恢复，却未在删除后的
正常历史读取入口验证。要求用显式占位摘要保持身份，不造 transcript/segment，补该
入口及相关 CLI/MCP/Review 恢复路径回归，并核对 trim 不越过生成媒体范围。

### R4：生成媒体/sidecar 的安全发布与失败清理不满足已有契约（WP2）

主代理只在 .acceptance 临时目录构造合成文件并复现：

- companion 目录是 symlink 时 `_publish_placeholder_companion` 接受并写入链接目标。
- 返回 created 列表后，将其中一个文件替换为其他内容，`_remove_owned_companion`
  仍将该文件删除（未验证本 action 的 hash/inode/identity）。

代码检查还发现：生成媒体和 companion 使用可预测 PID 临时名及 os.replace，无法
保证竞争情况下 no-replace/不覆盖；companion 复用未校验 hardlink。生成视频发布与
record 发布之间失败会留下无 record 的最终媒体，下一次 ensure 无法恢复。

要求：复用现有文件发布/锁/原子写入能力，小范围补齐安全父目录、唯一 owned staging、
no-replace、hash/inode readback、失败只清理本 action 仍拥有的产物；冲突保留现场并
准确报告，禁止删掉已改变或其他 action 复用的文件。并发 ensure/导出、receipt 写入
失败、record 发布失败、symlink/hardlink/替换后的清理须有实质回归。

### R5：新增类型与 lint 错误尚未收尾

`PYTHONPATH=core/src:. core/.venv/bin/python -m mypy core/src/roughcut --no-incremental`
报 20 项。相同命令对 git archive 导出的原 HEAD 源码报 2 项（mcp.py/cli.py 的
TextIO.reconfigure，基线文件 `.acceptance/mypy-baseline.txt`）。本次新增 18 项，
涉及 union narrowing、edit history、restoration、agent revision context、render plan
和 FFmpeg 参数；其中历史问题已是实际运行错误。

对已变更 Python source + 两个新增 source 运行 `python -m ruff check` 报 18 项；
包含本次新增 unused imports/import ordering 等，也夹有原有告警。
要求修复本次新增项，类型收窄应真实反映分支，不使用 Any/ignore 批量遮盖，不顺手
修改基线无关问题；结果文档记录真实命令和基线剩余告警。

## 其他裁决与环境事实

- JS 单 chunk 117,272B，相比原 114,970B 增加约 2.3KB；预算增加 5KB可接受，
  前提仍为无框架、可复现构建且不会继续为修正无限扩大。不是用户体验验收的替代项。
- 本机 /Applications 未发现 Final Cut Pro 或 Premiere；不安装软件、不声称实机导入
  已通过。保留待实机导入，交付短小样以便后续验证。
- 新合成测试约 1,240 行，包含完整工作流装配；本轮不要求重构测试框架。

执行代理修正后更新施工结果，补实际命令和回归结果；主代理收到完整报告再进行下一轮
独立复核。等待继续使用 Paseo notifyOnFinish，不轮询状态。

## 第二轮独立复核（2026-10-16）

- `git diff --check`：通过。
- 前端 `tsc --noEmit && vitest run`：32 files / 195 tests 通过，31.23s。
- Python 合成全链 + test_placeholder_publishing.py + 生产 W5 smoke：14 passed，32.19s。
- mypy：仅剩原 HEAD 的两个 reconfigure 基线错误（checked 111 source files）。
- 实际抽帧确认黑底中文“解说待配音”显示正常；合成 report 有 17 项检查。
- byline 真实 DOM 测试已验证标签/片段交错顺序、UTF-16、拖选、拖放与 undo。
  其 mixed-bylines.png 实际截图在 undo 之后，展示的是“人物丙”单标签；需补保存 undo
  之前的真正混合人物画面，并去掉测试里的 DEBUG-SEL 调试输出。

**仍未通过：R4 的确定性并发边界。**

主代理用两个线程及 Barrier/Event 固定如下顺序：A 先发布最终视频但尚未写 record；
B 发布输掉后先写 record；A 再写 record。当前两个 ensure 都声称成功，但 B 的 record
保存的是自己 staging 文件的 fingerprint（mtime），不是最终胜出的 A 文件。
结果：`record_fingerprint_matches_published_file False`；再次 ensure 报
`narration placeholder media fingerprint has changed`。

复现脚本：`.acceptance/independent-race-check.py`，只操作本任务临时目录，使用真实
FFmpeg。当前缺陷独立复现耗时 0.27s。要求新增该确定时序的回归，并验证第三次 ensure
及生成 SourceAsset 的 fingerprint 正常。最小修正应在 FileExists 分支采用并验证最终
已发布文件的 fingerprint/record，不能把 losing staging 的 fingerprint 写入记录。
`_adopt_orphan_media` 也应返回 `_write_new_record` 采用的 canonical record，避免并发
下返回不同于持久记录的候选对象。

## 第三轮独立复核（2026-10-16）

并发确定性脚本：generation_errors=[] / results=2，所有 record fingerprint 与最终
文件一致，subsequent_ensure=passed。R4 该缺口关闭。

主代理重新独立执行：

- TypeScript + 前端：32 files / 195 tests 通过（含真实 Chromium），27.22s。
- Python 按目录独立进程运行，避免现有同名测试模块收集冲突：
  domain 323 passed / 0.51s；application 1026 passed、14 skipped / 228.55s；
  adapters 867 passed、9 skipped / 23.13s；review 116 passed / 91.00s；
  integration 53 passed / 48.03s；e2e 4 passed / 24.58s；生产 W5 smoke 1 passed / 1.96s。
  共 2390 passed、23 skipped；日志位于 `.acceptance/independent-final/`。
- 本次四组公开 contract：16 passed / 1.60s。
- 静态体积及独立离线重建字节一致性：2 passed / 0.40s。
- mypy 仍仅两项既有 reconfigure 告警；变更 source 的 ruff 仍仅三项既有告警。
- 另做旧 narration/RenderPlan/parallel summary/NLE receipt 的四项直接回读：全部通过。
- 实际查看 mixed-bylines.png：乙标签/乙正文、甲标签/甲😀正文、乙标签/乙正文正确交错。
- 生产 JS=118171B，全部 static=171676B，均在已裁决预算内。

**剩余交付问题 R6：最终小样预览命令不成立。**

按施工结果文档实际执行：

`core/.venv/bin/roughcut review .acceptance/narration-placeholder-sample/project --run-id wfr_sample --json`

返回 exit=2 / workflow_integrity_error。直接 workflow_status 的原因是：
`Roughcut multicam continuation rejected durable state: continuation expected revision does not match the Project`。

主工程在 sample 末尾被 change_edit/delete/restore/undo/redo/trim 等测试改变了 revision，
而 run 的 exact continuation/adoption 仍绑定原版状态。自动测试只回读 edit snapshot，
未验证交付后真实 CLI run 入口。因此当前不能把该小样称作可直接打开的最终交付。

要求：把历史/trim 等会改变最终 run 绑定状态的检查放在样品副本，主样品保留正式完成
链路的 current 状态；禁止回写旧 revision/ref、放宽 stale/continuation 校验或移除行为测试。
新增最后实际 CLI 启动及 HTTP 200 检查，再更新施工结果。仅需修正验收 fixture，不改
生产状态机；无需再次无差别运行全部测试，主代理随后复验实际页面。

## 最终独立验收（2026-10-16）

R6 修正仅涉及验收 fixture 和施工记录。主代理独立执行：

```sh
PYTHONPATH=core/src:. core/.venv/bin/python -m pytest core/tests/integration/test_narration_placeholder_acceptance.py -q
```

结果：**1 passed in 16.53s**。重建后的 `report.json` 有 19 项检查；历史、trim、
删除恢复及 undo/redo 在 `lineage-project` 副本执行，主样品的正式工作流绑定未改变。
测试保留真实 CLI 启动、会话 cookie、首页及 `/api/review` 200 检查。

随后主代理在测试之外实际运行文档命令：

```sh
core/.venv/bin/roughcut review .acceptance/narration-placeholder-sample/project --run-id wfr_sample --json
```

CLI 返回 `ok=true` 并保持服务运行。使用本机真实 Chromium 访问生产静态页面，首页及
`/api/review` 均为 200；basis=decision，5 个片段，总时长 14 秒。
点击正文中的“这段解说还没有配音。”，播放器切换到 `/media/phm_*`，成功加载
1920×1080 占位视频（readyState=4），实际播放推进至 0.210817 秒；视频无 error，
页面无 pageerror。目视截图确认播放器内黑底白字“解说待配音”，正文和 14 秒总时长正常。
验收后已关闭测试浏览器和本次启动的 CLI 服务。

主代理证据：

- `.acceptance/independent-final/production-preview-browser.json`：页面、API、实际播放结果。
- `.acceptance/independent-final/production-preview-review.json`：生产 API 快照。
- `.acceptance/independent-visual/final-production-preview.png`：实际播放占位时的页面。
- `.acceptance/narration-placeholder-sample/report.json`：19 项小样检查；同目录包含主副
  MP4、两种 XML 及随交付占位媒体。

连同上一轮最终生产代码回归，独立结果为 **2408 项 Python 通过、23 项跳过；
195 项前端通过（含 42 项真实 Chromium 测试）**，TypeScript 检查通过、静态构建字节
一致；新增 mypy/ruff 错误为零，仍有原基线 2 项 mypy/3 项 ruff 告警。R6 后生产代码
未变，本轮只重跑受影响的小样并检查真实页面。最终 `git diff --check` 通过。

WP1 的人物切换位置、未知编号、选择/拖放/撤销；WP2 的确认、预览、占位总时长、
主副同步、共享媒体、两种 XML 结构及 companion、旧数据兼容和失败/并发行为，均有
上述独立证据。FCPXML/Premiere XML 的**目标软件实际导入仍待验证**，自动化 XML 与
文件检查不能替代该项。本机无对应软件；短小样已备好，无需为此重跑完整真实素材。

分支仍为 `evaluate-roughcut-narration-placeholder`，未提交/推送；原有未跟踪
`core/uv.lock` 保留，不属于本轮交付。

## 合并前对照施工文档（2026-10-16）

用户追加授权提交 PR 并合并。已重新 fetch，`origin/main` 仍为施工基线 `2863355`。

| 条目 | 核对结果 |
| --- | --- |
| WP1 / A1 | 姓名/身份、去重、章节/解说重置、跨素材未知身份与混合人物位置均有后端及 DOM 回归 |
| A2 | 真实 DOM 选择、caret、拖放、局部刷新、undo；正文/refs 保持独立，搜索使用正文偏移 |
| WP2 / A3 | 人工时长保存、确认、唯一 Proposal/Decision、采用、生产预览、主副渲染和两种 XML 全链已验证 |
| A4 | 合并前补上开头、末尾、连续两个占位的确认→编译→timeline 全边界测试；现有真实 Chromium 覆盖占位播放切换；已录音/无解说回归通过 |
| A5 | 黑底中文字、静音、主副总帧数/切点一致，有真实 FFmpeg 输出证据 |
| A6 | XML 共享媒体、locator/companion、无 gap、主副时段及 A/V 已自动验证；目标软件实机导入待验证 |
| A7 | 旧 canonical/hash、历史、幂等、篡改、失败清理、确定性并发及 stale/CAS 已验证 |
| A8 | 补全时长向上整帧检查：24/25/30/50/60 与 24000/1001、30000/1001、60000/1001；现有真实素材不支持情形继续拒绝 |
| 范围与交付 | 契约/schema、生产 static、小样重建命令和结果文档齐全；无 TTS/ASR 扩展，无管线或对轨重构，无生成视频入库 |

本次对照发现的是 A4 显式边界证据缺口，已补三个参数化测试，生产实现无需修改。
同时扩展已有 A8 时长测试的帧率覆盖并补上缺失的 assert，清理施工测试中的一个
未使用 import。真实执行：

```sh
PYTHONPATH=core/src:. core/.venv/bin/python -m pytest core/tests/application/test_content_drafts.py core/tests/domain/test_content_draft_schema2.py -q
core/.venv/bin/python -m ruff check core/tests/application/test_content_drafts.py core/tests/domain/test_content_draft_schema2.py
git diff --check
```

结果：74 passed / 0.75s；Ruff 与 diff 检查通过。相对上一轮增加 3 项边界测试，
Python 独立验证覆盖合计 2411 项通过（非声称重新全量执行），前端仍 195 项通过。

**合并裁决：没有未完成的实现项或已知阻断合并的产品缺陷。** 实机导入是计划第 6 节
明确允许在无软件时保留的独立证据项，本次不冒充通过；PR 必须披露该限制。保留
14 秒小样供后续轻量导入，无需重跑真实长片。可按用户授权提交并合并，仍须核对 PR
检查结果，不绕过远端合并约束。
