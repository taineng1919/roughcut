# NLE Handoff 生产契约


This contract defines the production handoff projection and export boundary. FCPXML 1.14 has been exercised with Final Cut Pro 12.3 on macOS; FCP7 XML/xmeml v5 has been exercised with Adobe Premiere Pro 14.1.0 Build 116 on Windows. Compatibility is limited to those exact combinations. Vendor XML is not Core timeline truth.

## 1. 唯一生产数据流

```text
Adopted Edit Decision
  + original SourceAssets
  + project timebase / RationalRate
  + exact deliverable Alignment Artifact（仅多机位）
        ↓
  immutable in-memory NleHandoffTimeline
        ├─ roughcut_fcpxml_1_14 writer
        └─ roughcut_fcp7_xml_xmeml_v5 writer
```

`NleHandoffTimeline` 只存在于一次 prepare/write 调用的内存中，不写入新的
canonical Project JSON、Timeline artifact 或 vendor-neutral 文件。它不是新的编辑
真相、内部 NLE 或通用 NLE framework。

### 2.1 Core truth

- 内容剪辑唯一真相：当前 exact Adopted Edit Decision。
- 媒体唯一真相：当前 Project 中的 original SourceAsset 与其 locator/fingerprint。
- 时间基准：Project `timebase` 和 `frame_rate`，核心计算始终使用整数 ticks 与
  `RationalRate`，不使用 float 秒。
- 多机位同步唯一真相：当前 Project/run 可验证的 exact deliverable Alignment
  Artifact。M4 不重新对齐、估 offset、从 MP4/parallel MP4 反推，或建立第二套 mapping。
- 单机位不读取、创建、补齐或伪造 Alignment。

### 2.2 最小 in-memory representation

生产表示由 frozen dataclasses 组成：

- `NleHandoffTimeline`：Project ID、`timebase`、`RationalRate`、项目宽高、音频采样率、
  总 duration ticks、ordered logical tracks。
- `NleHandoffTrack`：stable `track_id`、camera identity、`main|auxiliary` role、
  video/audio capabilities，以及按 timeline 顺序排列的 clip/gap items。
- `NleHandoffClip`：logical clip ID、SourceAsset ID、已验证的 original locator、
  source/timeline 半开区间、camera/track identity、video/audio capabilities、稳定
  A/V link identity，以及直接从 SourceAsset.probe 携带的 source width/height、nominal frame rate、VFR 标志和 audio sample rate。
  这些字段只存在于 transient handoff model；重复引用同一 Source 时由 Timeline 统一校验一致性。
- `NleHandoffGap`：stable gap identity、所属 track/camera、timeline 半开区间；它表示
  明确 absence，不携带黑媒体。

主轨每个 Decision clip 恰好生成一个 handoff clip，顺序完全一致，timeline 从 0
按整数 duration 累计。单机位保留每个 SourceAsset 原有 video/audio relationship。

多机位主轨仍完全来自 Decision；每个 auxiliary track 只把 Alignment 的 `mapped`
interval 投影为 original-source clip。`missing`、`uncertain`、`conflict` 及其余未映射
补集均生成 gap。不能可靠映射时不猜测、不插值、不自动补齐、不生成黑色视频或数字静音。
auxiliary audio 本体保留；不生成 native multicam object，也不自动切机或混音。

## 3. Frozen exporter profiles

只有以下两个 production profile：

| route | profile | output | status |
|---|---|---|---|
| `fcpxml` | `roughcut_fcpxml_1_14` | `.fcpxml` | 结构继承 FCP 12.3 Spike 修正；不泛化为其他 NLE/version verified |
| `fcp7_xml` | `roughcut_fcp7_xml_xmeml_v5` | `.xml` | xmeml v5 正式 writer；既有 Windows Premiere 14.1.0 Build 116 acceptance PASS；不扩展到其他版本/平台 |

用户不能选择其他 FCPXML 版本、xmeml 版本、编码器、lane、track 或 XML tuning。
两条 writer 共享同一个 projection；writer 不读取 ProjectStore、WorkflowStore、
AlignmentStore、Decision 文件或 ParallelRender artifact。

### 3.1 FCPXML 1.14

- 根元素为 `fcpxml version="1.14"`，资源通过 `asset` + `media-rep kind="original-media"`
  引用 SourceAsset locator。
- sequence 的 project format 只来自 Project rate/width/height；每个 fixed-rate video
  SourceAsset 使用按 probe 的 nominal frame rate/width/height 建立或复用的 source format，
  并由 `asset@format` 引用。source format 不冒充 project format。
- timeline 中的 main/auxiliary `asset-clip@format` 显式引用各自 SourceAsset 的
  source-specific format；audio-only clip 使用 `FFFrameRateUndefined` format。
  `start` 使用该 clip 的 source/local frame grid，`duration` 使用其 parent timeline 的
  projected project-grid extent，`offset` 使用 Apple 定义的 parent/base-element coordinate。
  这些值都以 rational seconds 输出，不把 source frame rate 重解释成 Project truth。
- 未知的 `audioChannels`、`audioSources`、`videoSources` 不输出；仅输出 probe 已知的
  `hasAudio`/`hasVideo` 和 `audioRate`。audio-only asset 使用官方 `FFFrameRateUndefined`
  format；未知值不以默认声道或 source count 填充。
- VFR video、缺失 fixed source rate/尺寸或不能安全表达的 source metadata fail closed，
  不把 nominal CFR 写成 VFR 的事实。
- main clips 是 primary storyline spine 的直接 `asset-clip`。
- auxiliary mapped clips 是挂在对应 main `asset-clip` 下的合法 connected/anchored
  `asset-clip lane="1..N"`；primary spine 禁止直接包含 nested `spine`。
- unmapped 区间不写 auxiliary clip，也不写 gap generator、black media 或 rendered MP4。

### 3.1.1 M4.5 corrective target-grid projection

M4.5 的 production blocker corrective implementation supersedes only the
FCPXML timing representation choice above; it does not change the canonical
`NleHandoffTimeline`, Decision, SourceAsset or Alignment truth, and it does not
change FCP7 XML semantics. The earlier M4.4 synthetic acceptance and its
historical record remain unchanged.

The FCPXML writer now builds one absolute main-boundary vector
`B = (0, main_clip_1.timeline_out, ..., timeline.duration)`, rounds every
boundary once with the existing round-half-up policy to the Project frame grid,
and derives each main `offset` and `duration` from adjacent projected
boundaries. The sequence duration is the projected final boundary. A collapsed
or inverted projected clip fails closed; no clip duration is independently
rounded or forced to one frame.

For every fixed-rate video clip, a main `start` is rounded nearest/half-up on
that SourceAsset's source frame grid. A connected child is different: its
`offset` and source `start` are one constrained representation pair. The
parent-local raw anchor is first formed from the projected parent source start
and projected timeline positions, then rounded nearest/half-up to the Project
frame grid:

```text
raw_offset = parent projected source start
             + child projected timeline in
             - parent projected timeline in
offset = project_frame_boundary(raw_offset)

canonical_delta = child.source_in
                  - (parent.source_in
                     + child.timeline_in
                     - parent.timeline_in)
desired_child_start = offset + canonical_delta
start = source_frame_boundary(desired_child_start)
```

The connected `offset` is therefore validated on `timeline.frame_rate`, never
on the parent SourceAsset grid. For a fixed-rate child, the serialized
alignment error must satisfy `abs(sync_error) <= half a child source frame`.
The writer checks the serialized start plus shared projected duration is
non-negative, stays within the original Source duration, and ends on the
child source grid. It never clamps, changes playback speed or writes rounded
values back to Alignment. Main and auxiliary clips representing the same
projected Decision interval reuse the same projected timeline boundaries and
therefore the same 1:1 duration. Source-start deltas and relative-sync error
remain transient diagnostics only; they are never persisted as a second
mapping.

Final Cut Pro 12.3 real-project evidence after the first corrective showed
main timing violations, auxiliary starts and durations clean, but 91 auxiliary
connected offsets still not on the 25 fps Project grid. That evidence
supersedes the first corrective assumption that a connected offset should be
validated on the parent/source-format grid. This second corrective changes
only that representation projection; it does not alter the canonical truth.

### 3.2 FCP7 XML / xmeml v5

- 根元素为 `xmeml version="5"`。
- sequence rate、`clipitem/rate`、`clipitem start/end/duration` 使用 Project sequence rate；
  `file/rate`、file duration 和 file 内 video sample characteristics rate 使用
  SourceAsset.probe 的 fixed nominal source rate。source width/height 也只来自 probe。
- Apple archived xmeml 语义规定 sequence 中的 `in`/`out` 按 sequence rate 表达；mixed-rate
  source 的 source-frame residual 通过 `mixedratesoffset` 保留。实现以 source exact frame
  为真相，按 sequence grid 取基准 `in/out`，不把所有字段 blanket 替换成 source rate。
- source boundaries 必须 exact source-frame aligned；source duration 必须能表达为 source
  frames，clip duration 必须能精确落在 sequence grid。不能表达、量化为零帧或 VFR 的 clip
  fail closed。
- timeline `start/end` 使用全局绝对 ticks 的 half-up boundary quantization，不逐 clip
  累积 rounding；gap 由辅助 track 缺少 clipitem 表达，不生成 generator/black clip。
- 同一 logical clip 的 video/audio clipitems 使用 reciprocal `link` 与稳定 group identity，
  保留原有 A/V relationship。
- `25/1`、`24/1` 使用 integer timebase + `ntsc=FALSE`；`30000/1001`、`24000/1001`
  使用 `30/24` + `ntsc=TRUE`；source rate 按同一规则独立编码。

## 4. 独立 Core-owned NLE approval boundary

现有 `approve_export` 仍只批准并执行 MP4 Render Plan；它不批准 NLE 文件。
普通 Decision adoption、粗剪采用、MP4 approval 或历史 receipt 都不自动授权 handoff。

M4 新增一个薄的、独立的 `approve_nle_export` action/application entry。一次调用同时
承载用户明确批准后的 exact action context 与同步写入，但拥有自己的 request hash、
receipt 和 idempotency namespace，不进入既有九项 finite-workflow action enum，也不改变
WorkflowRun stage/lifecycle。

调用必须绑定：

- `project_path`、exact `run_id`、fresh `action_id`；
- exact current Adopted Decision `edit_version_id`、schema version、content hash；
- current Project revision；
- current SourceAsset snapshot bundle（每个 source 的 snapshot hash、fingerprint、
  duration、locator identity）；
- route 与 Core 固定 exporter profile；
- canonical destination/output identity；
- 单机位 `alignment_artifact_id: null`；多机位 exact Alignment ID 与 content hash。

Core 只接受 active `export_review` 或已完成 MP4 export 的 current `exporting` run，且
必须重新验证 current adoption、Project/source snapshot、源文件 fingerprint、Alignment
producer/deliverable 状态和 destination。改变任一关键输入（Decision、revision、source
snapshot、route/profile、destination 或 Alignment）都会使旧 action/receipt 失效；同
`action_id` 不得对新的 request 复用。

输出安全规则：

1. destination 必须明确、规范化、父目录安全且不能是已有文件、symlink 或 hardlink；
2. writer 先返回 deterministic UTF-8 bytes，application 在 destination 同目录写入
   action-owned temp，flush/fsync 后以 no-replace atomic publish；
3. 仅最终文件完成 hash readback 后写 immutable handoff receipt；receipt 自身也采用
   no-replace atomic write；
4. 同 action/同 request 只回读已有 receipt 与同 hash output；同 action/不同 request、
   output conflict、stale truth、writer/publish/receipt failure 均 fail closed；
5. 失败不产生成功 receipt。若 publish 后 receipt 写入失败，只能在 output 仍与本 action
   的 hash/inode 完全一致时清理；否则保留现场并报告 recovery conflict，不声称成功。

这不是第二套复杂审批系统，也不让 Host/Agent 自行计算批准 hash。用户/Agent 必须在
调用前展示 route、profile、destination、Decision/素材摘要和多机位 Alignment 摘要，并
取得明确的 NLE export approval；Core 负责绑定与拒绝错绑。

## 5. CLI/MCP surface

固定公开入口：

```text
roughcut approve-nle-export \
  --project <path> --run-id <id> --action-id <id> \
  --edit-version-id <id> --expected-revision <revision> \
  --route <fcpxml|fcp7_xml> --destination <path> \
  [--alignment-artifact-id <id>] --json
```

MCP 等价工具为 `approve_nle_export`，使用相同字段和 closed schema；省略
`alignment_artifact_id` 等价于 JSON `null`，但多机位 run 必须提供非空 exact ID。入口
不接受版本选择、绝对 source locator、rendered MP4、ParallelRender ref、native
multicam 或 vendor 私有字段。

公开响应只报告 route/profile、项目/Decision/Alignment identity 摘要、destination 和
receipt/output hash 等必要诊断，不把 XML 当作新的业务 artifact；原始媒体路径不写入
提交、测试快照或普通日志。

## 6. 明确不进入本轮

- 不重新实现 Alignment，不从 rendered MP4 或 parallel MP4 反推；
- 不生成 native `mc-clip`；
- 不建设 Jianying private/native draft；
- 不建设 AAF、OTIO canonical model、通用 NLE framework、plugin/registry 或 vendor DSL；
- 不复制 Spike synthetic MP4、大型 fixture 或 Spike writer 到 production branch；
- 不进入新的 M4.5 real-project validation 或 M4.6 target-application revalidation；W7 仅做
  Windows artifact-generation/static validation，不改变 M4.6 的既有 acceptance。
