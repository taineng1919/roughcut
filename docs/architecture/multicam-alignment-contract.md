# 多机位对轨生产契约


当前实现使用固定 Audalign CorrelationRecognizer 写入对轨结果。macOS 多机位能力按支持范围提供；Windows 的多机位入口按现有错误码拒绝。自然停机/重启多文件真实行为尚未验证。协议示例见 `core/tests/fixtures/multicam-alignment-vectors.json`。

## 0. 平台拆分发布冻结

历史 schema 26 公共面保持不变，但发布按平台拆分：上述固定候选已通过 macOS `0.1.13`
source-checkout 平台切片发布门，Windows M2.7 暂缓。Windows 只对以下三个 direct CLI/MCP
entry 执行 release guard：`align_multicam` 使用既有
`alignment_runtime_unavailable`，`multicam_parallel_render_prepare` 和
`multicam_parallel_render_start` 使用既有 `parallel_render_runtime_unavailable`。既有 schema-25
单轨工具、ASR、Proxy、Draft/Review、Render、workflow action 和 `media_operation_status` 不受影响。
pure prepare closed union 只增加已存在的 `parallel_render_runtime_unavailable`；不新增 schema、
status 或 error code。

core 只实现一个 M2.7 专用 public-capability guard：
`require_m2_7_public_capability(entry_name)`，由 CLI/MCP 共用。transport 必须先完成该入口既有的
closed request shape/type 校验，再调用 guard；guard 通过前不得调用 M2.7 application、打开 Project、
读取用户媒体、访问 alignment/parallel staging、创建 OperationRecord 或启动 worker/child。不得在
CLI/MCP 复制平台判断，不得增加环境变量旁路、测试专用生产开关、通用平台/filesystem adapter、
retry、polling 或第二套 runtime manager。



当前平台边界：macOS 支持对轨和平行输出；Windows 的三个直接入口及自动 continuation 按现有错误码 fail closed。Windows status/readback 使用持久 operation identity，返回 `alignment_runtime_unavailable`，不创建虚假的 Alignment operation。

## 1. 审计基线与非目标

现有生产实现已经提供：

- Project schema 1 中不可变 `SourceAsset` 身份；`SourceFingerprint` 恰好是
  `size/mtime_ns/sha256_head_tail`，head/tail 算法读取最多首尾各 1 MiB，并把文件 size
  纳入摘要；
- Project-media OperationRecord schema 1、existing-first、per-operation writer/child lock、
  `pending/running/succeeded/failed/interrupted` 和纯 `media_operation_status`；
- component full plan/apply、installation OperationRecord、managed manifest 和持久
  `runtime.json`；
- `approve_export` 的 core-owned export ref、用户独立批准、受控 staging、export claim、
  TransactionMarker、exact-before rollback 和原子发布；
- 当前生产 tool schema 32、九个 workflow action/input 和五份 byte-identical canonical
  Skill tool-contract mirror；schema 31 是历史 Outline-widening contract state，schema 30 是历史 NLE/tool-surface contract state，schema 29 是历史 additive contract state，schema 26 是历史 M2.7
  public introduction，均只作兼容/readback 记录。

M2.7 复用这些边界，不修改 Project、WorkflowRun、Transcript、Content Draft、Proposal、
Decision、Proxy、现有 Render Plan/manifest、ApprovalRecord、ActionReceipt 或 TransactionMarker
schema。它不增加 ASR、场景组、匹配图、多轨编辑器、数据库、队列、scheduler、daemon、
heartbeat、PID lease、自动重试、续跑、worker 恢复、算法 registry 或 backend framework。

## 2. Audalign 与 runtime 冻结

### 2.0 当前 Core 0.2.8 Correlation writer

当前新写入唯一使用持久 RuntimeBinding 中通过同一 `validate_audalign_selection` 的
`audalign==1.3.1`、upstream commit
`d5955ae8a85b1cd480dadd005c3f88986f4ebbef`、managed `CorrelationRecognizer` selection。
BBC selection、缺失 selection、provider/version/upstream mismatch 和不完整 managed closure
均在 worker/子进程/临时 workspace 创建前 fail closed；已有 exact operation ID 的历史
readback 在安全加载当前 Project identity/scope 后、任何 Source/runtime/budget validation 之前返回。

当前 CorrelationConfig 完整身份只由 `AUDALIGN_CORRELATION_WRITER_PROFILE` 产生，并以
`canonical_sha256_v1` 形成 writer identity。其值直接对应 pinned 1.3.1 wheel 的实际默认值：
`sample_rate=8000`、`fft_window_size=4096`、`filter_matches=0.0`、`freq_threshold=200`、
`normalize=True`、`locality=None`、`max_lags=None`、`match_len_filter=None`、
`close_seconds_filter=None`、`locality_filter_prop=None`、`start_end=None`、
`start_end_against=None`、`passthrough_args={}`、`fail_on_decode_error=True`、固定
`cant_read_extensions`/`cant_write_extensions`，以及 `DEFAULT_OVERLAP_RATIO=0.5`、
`LOCALITY_OVERLAP_RATIO=0.5`、`DEFAULT_LOCALITY_FILTER_PROP=0.6`、
`SCALING_16_BIT=65536`、`multiprocessing=True`、`num_processors=None` 和 `plot=False`。
调用不接受 finder/config override；worker 从 parent 接收该 canonical typed config，构造
pinned recognizer 后逐字段比对，config 漂移直接返回非零。

输入只按 caller-declared exact `source_pairs` 执行。每个 unique main Source 只 decode 一次
为 44.1 kHz、mono、PCM16 LE full WAV；每个 pair 的 auxiliary 只保留一个 15 秒 bounded
excerpt。probe start 是 auxiliary duration 的整数 ticks 的 20%、50%、80% floor 后 clamp
到 `[0, duration - 1_800_000]`；短 source、重复 start 或非法 schedule 没有有效 probe，
直接得到 `uncertain`。三 probe 按 20→50→80 串行执行，`target=aux excerpt`、
`against=full main`，每个 `delta` 从 worker 的十进制文本按 ties-away-from-zero 转换，
`B = 0 - auxiliary_probe_start + seconds_to_ticks(delta)`。不运行 L/R second pass、
full-full、preprocess/noisereduce/torch、waveform/BBC/fingerprint fallback 或 score admission。

有效 B 只接受唯一 non-chaining cluster：至少两个不同 probe，diameter/spread 不超过
`12000 ticks`；多 cluster、chaining、无 candidate、worker/decode failure 和 no-overlap 均
fail closed 为对应 `uncertain`，worker failure 的 pair 不得因另外两个 probe 一致而 mapped。
representative 是 winning cluster 中按 20→50→80 的第一个 probe；跨 pair owner 的一致关系
按 deterministic source ID 选择，差异超过 `12000 ticks` 生成 Correlation conflict。
当前写入 identity 是 `roughcut_audalign_correlation_fixed_offset` v1、algorithm
`audalign_correlation`/`1.3.1`、evidence family `roughcut_audalign_correlation_evidence_v1`；
完整 writer profile、selection、source basis 和 integer workspace estimate 进入 input hash。

以下 2.1–2.5 保留的是历史 Fingerprint/profile 1/2、旧 closure 和当时的验收事实；它们只
支撑旧 exact-ID artifact/request readback，不改变上述当前 writer。

### 2.1 历史唯一 fingerprint 实现（仅 readback）

历史生产候选固定为 `audalign==1.3.1`，上游 tag `v1.3.1` 的 commit 是
`d5955ae8a85b1cd480dadd005c3f88986f4ebbef`，许可证 MIT。只调用公开
`FingerprintRecognizer`/`audalign.recognize(target, against, recognizer=...)`，固定
`accuracy=2`、`num_processors=1`。根据该版本 README、Recognition Results Explanation 和
固定 source：

- `match_info[against]` 的 `offset_seconds`、`confidence` 和 `locality_seconds` 是候选证据；
- Fingerprint confidence 是匹配 fingerprint 数量，rank 只是强度提示，不是正确性证明；
- 正 offset 表示 against file 在 target file 之后开始；core 必须结合调用角色换算到下述
  source ticks，不能把工具的浮点秒或排序直接当成业务真相；
- 无结果、候选不唯一或独立窗口不一致必须 fail closed；不设上游没有定义的 candidate 数量门。

本次只读审计的官方文本身份如下，Phase 2 实现不得凭记忆替换其语义：tag README content
SHA-256 `1e425c777862049059af7096ae28e106c6a3b79fdcf96c84205fdaa8a44119da`；wiki
Recognition Results Explanation（核对时 HEAD
`5e8b5975948fb2ea23b0737e413b7fcd48824cb5`）content SHA-256
`d433fb86bfdb2068548f74fab0e0b378a1a33edbdf13481a9016223891b29bd4`；tag source
`audalign/__init__.py`、`audalign/config/fingerprint.py`、
`audalign/recognizers/fingerprint/__init__.py`、`fingerprinter.py`、`recognize.py` 的 content
SHA-256 依次为 `d17d58eea9d80903146c98d18caf89db72fbca4fb2c218e204f69decfb35476d`、
`d6551cdaea14c31a58b2dbd42d10d61ac30517e8f4de15305bd9369ae80de119`、
`d323a4d3c4ddf1568bdce796f89a067aeb64fbe364311002c35aff09f9bc2f25`、
`27696cb287d67ef7fac1c8acf1c9c66e07c41c63c8062eca0f0e9a14e2e53c03`、
`6c34d0e03b2f9bf2e1fa9e552ed5757a72af0d35019f42db6db0ab34b012060a`。

BBC `audio-offset-finder` 不启动；FFmpeg correlation 不是第二个召回后端；当前手写 Spike
fingerprint 不进入生产。默认先把每个 Source 的第一音频流混为 mono；只有默认结果无法形成
唯一安全候选时，才有界尝试 L/R，且两个被接纳声道必须支持同一 ticks 关系。副机位不 ASR。

### 2.2 历史 Fixed-offset verification profile 1

唯一 closed profile 名为 `roughcut_audalign_fixed_offset`、版本为 `1`。所有业务时间使用
Roughcut 全局时间基 `120000 ticks/s`，不得在 alignment artifact、vectors 或投影中引入第二
时间基。Audalign 调用角色固定为 `target=auxiliary excerpt/window`、`against=main
reference/window`。上游返回 `offset_seconds=Δ` 时，正值表示 against 比 target 晚开始；若
auxiliary excerpt 的 source-local 起点为 `A0` ticks、main reference 的 source-local 起点为
`M0` ticks，则等速 source 关系为：

```text
main_tick = auxiliary_tick + B
B = M0 - A0 + seconds_to_ticks(Δ)
```

该加号直接来自 v1.3.1 fingerprint source 的
`sample_difference = against_offset - target_offset`，不是调用方猜测。

`seconds_to_ticks` 只执行一次十进制换算：以外部结果的十进制文本构造精确 Decimal，乘
`120000` 后按 nearest、ties away from zero；即正数 `floor(x + 0.5)`，负数
`ceil(x - 0.5)`。不得先以 binary float 做中间 rounding。

每个 raw candidate 都先换算为 `B`，按数值升序分组；同组最大值减最小值不得超过
`12000 ticks`（0.10 秒），不允许链式相邻合并扩大组径。对每个 candidate 分别在其预测的
main/auxiliary 有效重叠 `[s,e)` 内取三个 12 秒（`1440000 ticks`）且不重叠的 source-local
窗口：开头从 `s` 开始，结尾到 `e` 结束，中段起点为
`floor((s + e - 1440000) / 2)`。必须满足前窗结束不晚于中窗开始且中窗结束不晚于末窗开始；
有效重叠不足 36 秒时该 candidate closed reject。每个 aux window 仍作为 target、只按该
candidate 计算的 main window 作为 against，再调用同一 Audalign 1.3.1 public API；三次都必须
有 top result 且换算后的局部 offset 绝对值不超过 `12000 ticks`。没有 confidence 门，也不以
ground truth 参与分类。

零 raw candidate 为 `uncertain`；有 raw candidate 但零组通过为 `uncertain`；恰一组通过为
`mapped`；至少两组通过为 `conflict`。唯一组内只用上游原始排名最前的通过候选作为保存关系，
排名不参与是否 mapped 的决定。artifact 的 `algorithm` 与每条 evidence 都保存 profile/version；
`input_hash` 绑定上述完整 profile。Phase 2 验证仍只能调用 Audalign 1.3.1 public API，不增加
相关搜索、第二算法或调参框架。

### 2.3 历史精确闭包、许可证和资源上限

CPython 3.11 无 extras 的固定闭包为：`audalign 1.3.1`、`matplotlib 3.8.2`、
`numpy 1.26.4`、`pydub 0.25.1`、`scipy 1.12.0`、`setuptools 59.6.0`、`tqdm 4.66.2`、
`contourpy 1.3.3`、`cycler 0.12.1`、`fonttools 4.63.0`、`kiwisolver 1.5.0`、
`packaging 26.2`、`Pillow 12.3.0`、`pyparsing 3.3.2`、
`python-dateutil 2.9.0.post0`、`six 1.17.0`，以及仅 Windows 的 `colorama 0.4.6`。
每个平台的精确 wheel 文件名、bytes、SHA-256、许可证和 notice 文件以 rollout 第
11.2–11.4 节冻结表为本契约的规范性组成部分；任何一项变化必须重新走 component full plan
和用户批准，不能宽松解析版本。

Phase 2 生产 profile 的硬上限固定为：组件下载+安装工作根 512 MiB、单次分析临时空间
2 GiB、Audalign/FFmpeg 子进程树峰值 RSS 4 GiB、单次 `align_multicam` 运行 7,200 秒。
公开请求可选择更小的正整数硬预算，不能超过上述 ceiling。历史材料中出现的 64 秒是
synthetic `multi-first` 输入的长度，不是 Phase 0 样本 A 的真实 elapsed；没有保存可复现的真实
全长 elapsed operation。Phase 0 只证明 offset 关系和开头/中段/结尾三位置的人工同步正确，
没有证明全长生产 recall 性能。

Audalign child 的磁盘输出也属于上述 workspace ceiling：固定 commit 的
[`recognize.py`](https://github.com/benfmiller/audalign/blob/d5955ae8a85b1cd480dadd005c3f88986f4ebbef/audalign/recognizers/fingerprint/recognize.py) 不对
unique offset 数量设上限，故不能把 profile 2 的父进程候选检查当作 JSON 字节上界。worker
在写文件前对 profile 2 recall 做 512 candidate admission；任何超限都只写内部 overflow
marker，不截断候选。所有 Audalign response 还受 8 MiB 的完整 JSON 上限；adapter 解析成功
或 marker 后立即释放该 response，失败或无法解析的文件保留给 child 后 workspace recheck。
固定 commit 的 [`filehandler.read`](https://github.com/benfmiller/audalign/blob/d5955ae8a85b1cd480dadd005c3f88986f4ebbef/audalign/filehandler.py)
只有显式传入 `wrdestination` 才写音频文件，当前 recognize 路径未传入该参数；因此 Audalign
recognize 本身只保留内存输入/结果，worker 唯一的预期文件输出就是上述有界 JSON。代码回归还
以 3 个 recall excerpt、6 个复用的 verification window 和一个有界 response 验证总量低于
32 MiB overhead；runtime probe 同样逐 child recheck workspace。

### 2.4 历史 Fixed-offset recall profile 2（有界实现已合并，Mac 验收已完成）

真实 Mac 性能证据显示 profile 1 把长 auxiliary 与长 main 一次性送入 Audalign 时会在
`recognize_auxiliary` 触及冻结的 1,800 秒/4 GiB 资源边界。profile 2 只收窄 recall 输入和调用
计划，不改变 Audalign、accuracy、并行度、验证阈值或全局资源 ceiling。有界实现已合并并通过
独立代码评审；Mac 合成与授权真实素材验收、平行输出和项目发起人同步确认已完成；macOS
`0.1.13` source-checkout 发布门已只读复用这些事实，Windows M2.7 仍暂缓。

profile 2 的完整 canonical 参数如下，参数对象的字段顺序和数值属于 profile identity：

```json
{
  "name": "roughcut_audalign_fixed_offset",
  "version": 2,
  "audalign": {
    "package": "1.3.1",
    "upstream_commit": "d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
    "recognizer": "FingerprintRecognizer",
    "accuracy": 2,
    "num_processors": 1
  },
  "roles": {"target": "auxiliary_excerpt", "against": "main_complete_source"},
  "mapping_model": "fixed_offset_equal_speed",
  "ticks_per_second": 120000,
  "recall_excerpt_length_ticks": 1800000,
  "recall_probe_start_algorithm": "[0, floor((D - L) / 2), D - L], deduplicated in that order",
  "recall_probe_order": ["opening", "middle", "ending"],
  "max_recall_probes_per_channel": 3,
  "max_recall_calls_per_source_pair": 9,
  "max_raw_candidates_per_probe": 512,
  "max_selected_candidates_per_probe": 1,
  "max_candidate_groups_per_channel": 3,
  "max_verification_calls_per_source_pair": 27,
  "max_audalign_calls_per_source_pair": 36,
  "channel_schedule": {"mono": "same_probe_schedule", "left_right_fallback": "same_probe_schedule"},
  "candidate_group_diameter_ticks": 12000,
  "verification_window_ticks": 1440000,
  "verification_window_count": 3,
  "maximum_local_error_ticks": 12000
}
```

`D` 是当前 auxiliary Source 的 source-local duration，`L=1,800,000` ticks。Source 短于 `L`
时在 worker 前拒绝该 pair 并收敛为 `uncertain`；不会用一个不完整 excerpt 猜测覆盖。每个
pair 只按 opening → middle → ending 顺序执行 recall。默认 mono 最多 3 次；只有 mono 无法
形成唯一安全候选时，才按同一 schedule 依次尝试 left、right，fallback 的 recall 上限为 9 次。
每次 recall 使用同一完整 main Source，`M0=0`，不做 main tiling；profile 2 不引入 main tiling、
重叠或第二套顺序。多文件仍按用户请求中的 ordered Source pair 顺序执行，最大计划 Audalign
调用数为 `36 * pair_count`；超过 profile2 的 planned call、候选响应或候选组上限时拒绝继续，
不追加隐式 probe。

每个 channel/probe 的 Audalign response 按 closed admission 处理：零 candidate 不产生
hypothesis；`1..512` 个 candidate 只允许 upstream index `0` 进入 bounded hypothesis set；
超过 `512` 个 candidate 时该 channel 立即为 `uncertain`，verification 次数为零，response
不得截断或以其他 raw candidate 补救。upstream rank 只决定有限的 verification 对象，不决定
`mapped`；confidence 和 ground truth 都不参与 admission、grouping 或分类。

每个 raw candidate 都必须先用其所属 excerpt/reference 的真实 source-local 起点换算：

```text
B = M0 - A0 + seconds_to_ticks(Δ)
```

不能把窗口局部 offset 直接当作 source-global `B`。三个声道各自拥有独立的 candidate 流，
互不以另一个声道的结果覆盖。每个 probe 最多贡献一个 admitted hypothesis，因此每个声道
最多三个 hypothesis。每个声道的稳定总序固定为 probe 顺序
`opening → middle → ending`，再按 Audalign upstream index；admitted hypothesis 随后按
source-global `B` 分成直径不超过 `12000 ticks` 且不链式扩组的 group，相同 `B` 的多个 probe
合并到同一 group。每个声道最多三个 group；超过三个时该声道为 `uncertain`，verification
次数为零。每个 group 恰好选择稳定总序中的第一个 admitted hypothesis 作为 representative；
该 representative 的三个独立 verification window 任一失败即该 group 失败，不尝试该 probe
response 中其他 raw candidate。

默认 mono 流若得到唯一 verified `B`，立即以 mono 成功，不运行 left/right。mono 只有在非失败
但不能形成唯一安全 `B` 时，才按同一 probe schedule 运行 left、right；Audalign worker error、
decode error 和全局 budget error 按既有 per-camera/global failure 语义处理，不以“无唯一候选”
触发 fallback。left/right 各自完成上述独立分组与 verification 后，只有相同 `B` group 才能合并；
任一声道 conflict 或不同 verified `B` 保留为 pair-level conflict，不能被另一个 mapped 声道
覆盖。mono 与 left/right 支持同一 `B` 时只保存一个关系。零候选、opening 无覆盖、所有 probe
零候选和 planned budget exhausted 都是 `uncertain`，不伪造 `missing`。

verification 仍固定为三个独立、不重叠的 12 秒窗口，局部最大误差仍为 `12000 ticks`；recall
probe 不能替代 verification。按每声道最多三个 B group、最多三个声道证据计算，verification
最多 27 次，连同最多 9 次 recall 为每 pair 36 次 Audalign 调用。任何全局 disk/memory/time ceiling
超限仍是 operation-level failed、`result_ref=null`、零 alignment artifact；不得降级为
per-camera partial。pair-level 的 uncertain 不改变既有 `mapped/missing/uncertain/conflict`
分类和 camera summary 语义。

profile 1 artifact（`schema_version=1`、`verification_profile.version=1`）必须继续可读；任何
新 alignment 只能写 `verification_profile.version=2`，不得把 profile 2 冒充 profile 1，也不
修改 artifact schema version。profile 2 的完整参数对象同时进入 `input_hash` 的 canonical
preimage、algorithm identity 和每条 interval evidence 的 profile identity。artifact reader 只接受
closed profile version `1` 或 `2`，并分别解析到代码内定义的两个已知 canonical 常量；不得新增
通用 profile registry。reader 不从 digest 反推 preimage，也不以 digest alone 声称完整参数已被
证明进入 hash；producer 必须在构造 `input_hash` preimage 时实际包含本节完整 profile 2 参数。
profile 2 的每条 interval evidence 必须与已知 version-2 常量一致。交付路径还必须核对
`artifact.input_hash` 与 exact producer `OperationRecord.input_hash` 相等；不相等则 artifact
不可交付。上述约束不增加公开请求字段、artifact schema 字段或改变 schema 1。

### 2.5 历史 component/runtime 机制

Phase 2 只允许对现有 component catalog、full plan/apply、cache、managed manifest、
installation OperationRecord 和 persistent runtime binding 作加性接线：

1. component plan 把上述精确闭包作为一个不可拆分的 `audalign_fingerprint` managed group；
2. 它使用自己的 managed Python 3.11 venv，不能安装进 FunASR Python、系统 Python或项目
   `.venv`；但 plan/apply、批准 hash、下载验证、ownership、rollback 和 uninstall 仍是现有
   唯一实现；
3. persistent runtime binding schema 2 在 schema 1 全部字段之外增加 closed
   `alignment_python`，保存 canonical interpreter、Python/Audalign/依赖版本、固定 lock receipt
   和上游 commit；FFmpeg/ffprobe 继续使用同一 binding 的选择；
4. schema 1 binding 对既有能力保持可读；`align_multicam` 必须要求已完整验证的 schema 2
   alignment selection。临时环境变量或 CLI override 不能成为 tracked start 的组件真相。

若实现需要第二套 installer、runtime manager、下载器、daemon 或另一个 persistent binding
文件，Phase 2 必须停止架构复评，不能用文档命名掩盖重复机制。

`alignment_python` 的 Audalign provider 编码字段恰好为：

```json
{
  "source_type": "managed",
  "ownership": "roughcut_managed",
  "interpreter": "/canonical/managed/alignment-venv/bin/python",
  "python_version": "3.11",
  "audalign_version": "1.3.1",
  "audalign_upstream_commit": "d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
  "distributions": [{"name": "audalign", "version": "1.3.1"}],
  "dependency_lock_receipt": {"algorithm": "sha256", "value": "64-lowercase-hex"},
  "license_notice_receipt": {"algorithm": "sha256", "value": "64-lowercase-hex"},
  "component_manifest_receipt": {"algorithm": "sha256", "value": "64-lowercase-hex"}
}
```

2026-08-23 remediation B1 起该 selection 为 provider-aware：reader 额外接受显式
`provider`/`provider_version` 键的编码，并把仅含 `audalign_version` 的历史文件归一为
Audalign provider；两种编码同时出现时 fail closed。当前只有 Audalign 拥有 frozen managed
contract；任何其他 provider 在绑定校验层 fail closed，直到其 catalog/lock/license 契约
落地，Audalign 安装绝不充当等价 provider。Audalign 绑定的写入序列化保持上表逐字节不变，
既有 runtime.json 与 profile 2 input-hash preimage 因此稳定。

`distributions` 按 normalized package name 排序，恰好包含第 2.2 节的平台闭包：macOS 16 项，
Windows 17 项；不能多包、缺包或宽泛版本。interpreter 必须位于现有 managed root 内的
canonical 普通 executable，venv tree 不得是 symlink。三个 receipt 分别绑定冻结 lock、wheel
内实际许可证/notices 的逐字清单与内容、以及现有 component manifest；任一不符时 runtime 不可
用于 tracked start。

## 3. Multicam Alignment artifact schema 1

### 3.1 身份、存储与不可变性

唯一 Project-contained 路径是：

```text
<project>/artifacts/multicam-alignments/<alignment_id>.json
```

`alignment_id` 使用现有 safe-ID grammar。Project root、`artifacts/`、
`multicam-alignments/` 和目标必须是 canonical containment 内的普通目录/文件；目标和临时文件
拒绝 symlink、hardlink（`nlink != 1`）、非普通文件和 path escape。读取只接受调用方给出的
exact ID，不扫描目录、mtime、媒体文件或命名猜 artifact。

发布使用同目录唯一临时文件、`O_EXCL|O_NOFOLLOW`、0600、canonical JSON、file fsync、strict
schema readback、atomic no-replace publish、directory fsync 和 final exact readback。并发发布
同一 ID 时只有一个相同 bytes 结果可成功；不同 bytes 必须 `alignment_publish_conflict`，不得
覆盖。artifact 一经发布不可修改或删除。

### 3.2 Closed JSON shape

顶层字段恰好为：

```json
{
  "schema_version": 1,
  "alignment_id": "aln_safe-id",
  "project_id": "project_safe-id",
  "producer_operation_id": "op_uuid-v4-hex",
  "created_at": "RFC3339 UTC",
  "request_hash": "sha256",
  "input_hash": "sha256",
  "algorithm": {
    "name": "audalign_fingerprint",
    "version": "1.3.1",
    "upstream_commit": "d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
    "accuracy": 2,
    "num_processors": 1,
    "mapping_model": "fixed_offset_equal_speed",
    "ticks_per_second": 120000,
    "verification_profile": {"name": "roughcut_audalign_fixed_offset", "version": 1}
  },
  "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_main_1"]},
  "auxiliary_cameras": [],
  "source_basis": [],
  "intervals": [],
  "summary": {
    "total_main_ticks": 0,
    "camera_count": 0,
    "mapped_ticks": 0,
    "missing_ticks": 0,
    "uncertain_ticks": 0,
    "conflict_ticks": 0
  }
}
```

所有 object 拒绝未知字段；所有 ID 安全且在其数组内唯一。`created_at` 只用于审计，不参与
映射。artifact 不保存 Decision ID/ref，因此 Decision 改稿或重排只重新投影，不重新对轨。

`main_camera` 的 source 列表非空；`auxiliary_cameras` 非空且任意长度。每项恰好包含：

```json
{
  "camera_id": "aux-1",
  "ordered_source_ids": ["src_aux_1"],
  "status": "complete|partial|omitted|failed",
  "mapped_ticks": 0,
  "missing_ticks": 0,
  "uncertain_ticks": 0,
  "conflict_ticks": 0,
  "errors": [{"code": "auxiliary_decode_failed", "source_id": "src_aux_1|null"}]
}
```

`source_basis` 按 `(camera_id, source_id)` 排序，每项恰好保存
`camera_id/source_id/fingerprint/duration_ticks`；fingerprint 恰好是
`size/mtime_ns/sha256_head_tail`。它不保存 locator、display name、原文件名、绝对路径、
inode 或 name hash。所有输入 Source 必须已存在于 Project；执行前后 exact fingerprint、
regular-file/symlink 和 resolved identity 必须一致。source group 不能重复 source ID，主/副
group 不能重叠。

`producer_operation_id` 必须等于实际生成并原子发布该 artifact 的 `align_multicam`
OperationRecord ID；artifact → OperationRecord 的直接链不可为空或改绑。该 record 可以是
`succeeded`，也可以是在 artifact publish 后、record succeeded 前硬退出并最终收敛的
`interrupted`。只有 `succeeded` record 才能通过其 `result_ref` 对外交付 artifact；status 或
调用方不得从孤立 artifact 反推、接纳或修复成功。

### 3.3 区间与分类

每个 auxiliary camera 对每个 main Source 形成从 0 到 `duration_ticks` 的有序、无重叠、
无空洞 partition。每个 interval 恰好为：

```json
{
  "interval_id": "ali_safe-id",
  "auxiliary_camera_id": "aux-1",
  "classification": "mapped|missing|uncertain|conflict",
  "main": {"source_id": "src_main_1", "start_ticks": 0, "end_ticks": 120000},
  "auxiliary": {"source_id": "src_aux_1", "start_ticks": 30000, "end_ticks": 150000},
  "evidence": {
    "code": "fixed_offset_verified",
    "raw_candidate_count": 1,
    "matching_fingerprint_counts": [940],
    "verification_window_count": 3,
    "verification_profile": {"name": "roughcut_audalign_fixed_offset", "version": 1},
    "max_local_offset_error_ticks": 12000
  }
}
```

`end_ticks > start_ticks >= 0`，且不越过对应 Source duration。`mapped` 必须有一个
`auxiliary`，主/副长度必须完全相等，等价 slope 固定为 1；其余三类的 `auxiliary` 必须为
`null`。artifact 不保存浮点秒、drift、速度、DTW path 或通用时间变换。Audalign 浮点候选
只在首次执行内按统一 rounding 转为整数 ticks，再由独立开头/中段/结尾窗口验证；无法唯一
转成一个关系就分类为 `uncertain` 或 `conflict`。

`matching_fingerprint_counts` 只保存按 Audalign 原顺序取得的非负整数证据，不是通过阈值；
`verification_window_count` 只能是 0 或 3。`max_local_offset_error_ticks` 仅在三窗口都通过时为
非负整数，否则为 null。不存在“置信度百分比”。

分类和机位汇总恰好为：

- `mapped`：唯一候选通过三个独立窗口；
- `missing`：只有处理完该机位全部获授权 Source，且全部 Source 均成功 probe/decode/recognize/
  verify 后，依据已经唯一验证的 source 映射、每个 Source 精确 duration 和完整文件范围，能够
  正面证明该 main interval 不落入任何 auxiliary 覆盖时才成立；
- `uncertain`：无候选、弱候选、窗口失败、短于三窗口覆盖，或 probe/decode/recognition/
  verification 失败使该区间不可判断；同一机位存在任何未成功处理 Source 时，其可能覆盖的
  全部剩余范围必须是 uncertain，不能是 missing；
- `conflict`：两个或更多相差超过一个固定验证容差的候选分别通过；
- `complete`：该机位全部 main ticks 都 mapped 且 errors 为空；
- `partial`：至少一个 tick mapped，且仍有 non-mapped ticks 或 errors；
- `omitted`：零 mapped、无执行错误，全部由 missing/uncertain/conflict 安全拒绝；
- `failed`：零 mapped 且该机位发生执行错误。

文件/机位错误保留每一条 stable `code`，但不保存异常文本、stderr、命令、路径或原文件名。
一个失败不删除本机位或其他机位已完成的 mapped intervals。主机位输入、probe、decode、索引
失败和全局 disk/memory/time budget 超限仍是整个 operation 的硬失败，不发布 artifact。

summary 的 `camera_count` 等于 `auxiliary_cameras` 项数，四类 ticks 是 **camera-ticks**，不是
去重后的主时间线 ticks；每个 auxiliary camera 的四项之和等于全部 main Source duration 之和，
并且 artifact 必须满足：

```text
mapped_ticks + missing_ticks + uncertain_ticks + conflict_ticks
  == total_main_ticks * camera_count
```

alignment per-camera error code union 恰好为：`auxiliary_probe_failed`、
`auxiliary_decode_failed`、`auxiliary_recognition_failed`、`auxiliary_verification_failed`、
`auxiliary_audio_stream_unsupported`。不得写自由文本或占位 code。Source/Project/runtime basis
变化不是可隔离机位错误。

## 4. `align_multicam` OperationRecord

`align_multicam` 是唯一 alignment operation type。它作为 Project-media
OperationRecord schema 2 的一个 closed type，复用 schema 1 的 scope、existing-first、
status、writer/child lock、transition、`<project>/workflow/operations/media/<operation_id>.json`
store 路径和纯 `media_operation_status`；schema 1 records 保持逐字可读。schema 2 的 type
union 只增加 `align_multicam` 与第 5 节独立的 `render_multicam_parallel`，并为两者增加明确
result/error/phase union，不泛化 operation。

Host 在调用前同时持有新的 operation ID 和新的 alignment ID。start 的稳定公开请求恰好为：

```json
{
  "project_path": "path",
  "operation_id": "op_uuid-v4-hex",
  "alignment_id": "aln_safe-id",
  "expected_revision": 1,
  "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_main_1"]},
  "auxiliary_cameras": [{"camera_id": "aux-1", "ordered_source_ids": ["src_aux_1"]}],
  "main_audio_stable": true,
  "max_temporary_disk_bytes": 536870912,
  "max_analysis_memory_bytes": 4294967296,
  "max_runtime_seconds": 1800
}
```

`request_hash` 是去掉 `project_path` 后上述 closed public request 的 canonical hash；Project
scope 已绑定 canonical root。它不含当前 fingerprint、runtime、探针结果、内部阈值或临时路径。
任何同 ID 现有 record 必须先按 scope、type、request_hash 回读；相同请求只返回 exact record，
零 current Project/Source/runtime 检查、零 worker、零 mutation。不同请求返回
`operation_input_conflict`，零 worker/零 mutation。

只有 operation 缺失时才在写 pending 前验证 Project revision、全部 Source refs、group
唯一性、locator containment、regular/symlink、fingerprint、probe、预算和 persistent runtime。
`input_hash` 绑定首次执行的完整 basis：request projection、Project ID/revision、每个 ordered
Source 的 `source_id/import_mode/locator_identity_hash/fingerprint/probe`、resolved
dev/inode/regular/symlink evidence、runtime binding canonical hash、Audalign package/commit/
dependency-lock receipt、FFmpeg/ffprobe command identity/version/receipt、固定 mono-first/LR-
fallback、所选 `roughcut_audalign_fixed_offset` profile 的完整 canonical 参数和资源估算。
profile 1 绑定 version 1；profile 2 绑定 version 2 及第 2.4 节完整参数对象、probe schedule、
candidate/call ceilings 和 verification identity；同一 request 不能在两种 profile 间复用 input_hash
或 artifact。
record/report 不输出 locator 或绝对路径。

preflight 硬失败不创建 pending record、worker、workspace 或 artifact。pending 成功后，writer
lock 覆盖所有 FFmpeg/Audalign 子进程生命周期；主 source 先有界准备，随后按请求中的
auxiliary camera/source 顺序单进程执行。子文件失败收敛为脱敏区间/机位 error 并继续安全项；
全局预算、主机位或 artifact store 失败收敛 operation failed。已完成安全映射可发布 partial
artifact，随后 record succeeded；发布前再次核对全部 Source/runtime basis。

`alignment_revalidating_basis` 必须在 publish 前对 Project revision、每个主/副 Source 的 resolved
identity/regular/symlink/fingerprint 和 persistent runtime binding exact hash 全量重验。任一变化
都是 operation 级 failed：Project/Source 变化使用 `alignment_basis_changed_during_run`，runtime
变化使用 `alignment_runtime_changed_during_run`；不得发布 artifact，也不得降级为 per-camera
error。artifact 已发布而 record 尚未 succeeded 的硬退出不回滚 artifact：child 收敛后旧 record
变 interrupted，artifact 原样保留但无可交付 result ref。显式重跑必须由用户重新批准并提供新的
operation ID 与新的 alignment ID；不得接纳、扫描、删除或覆盖旧 artifact。

固定 phases 为 `alignment_preparing/alignment_decoding_main/alignment_indexing_main/
alignment_processing_auxiliary/alignment_revalidating_basis/alignment_publishing`；terminal codes 为
`alignment_succeeded/alignment_failed/alignment_interrupted`。成功 result ref 恰好保存
`kind=multicam_alignment/alignment_id/schema_version/content_hash`。响应丢失后 Host 只用同一
operation ID 调 `media_operation_status`；writer 消失时沿用既有 closed interrupted 语义，
不恢复 worker、不自动重试，用户若要重跑必须明确使用新 ID。status 只在 child 已终止并被
wait、writer lock 已释放后把遗留 running record 收敛为 `interrupted`；它不扫描 artifact/final、
不清理 workspace/staging，也不从文件存在推断成功。

alignment start/preflight 的 closed error union 恰好为：`operation_input_conflict`、
`alignment_input_stale`、`alignment_runtime_unavailable`、`alignment_main_probe_failed`、
`alignment_disk_budget_exceeded`、`alignment_memory_budget_exceeded`、
`alignment_time_budget_exceeded`；这些错误不创建 record。已创建 record 的 terminal error
code union 恰好为：`alignment_disk_budget_exceeded`、
`alignment_memory_budget_exceeded`、`alignment_time_budget_exceeded`、
`alignment_main_decode_failed`、`alignment_main_index_failed`、
`alignment_basis_changed_during_run`、`alignment_runtime_changed_during_run`、
`alignment_store_integrity_error`、`alignment_publish_conflict`、`alignment_publish_failed`、
`alignment_interrupted`。closed union 之外的异常不得写入 record。

### 4.1 Windows Job 内存强制与错误归因边界

Windows child tree 始终以 `JOB_OBJECT_LIMIT_JOB_MEMORY` 强制 exact
`4,294,967,296` bytes hard ceiling；`7,200` 秒 runtime ceiling 和 `2,147,483,648` bytes
(2 GiB) 临时磁盘 ceiling 同样保持不变。资源安全与错误归因是两个独立判断：hard ceiling 始终生效，
但只有父进程实际取得下列正证据之一时，才允许抛出 typed
`ChildProcessMemoryBudgetError` 并映射为全局 `alignment_memory_budget_exceeded`：

1. 父进程实际观察到 `PeakJobMemoryUsed >= 4,294,967,296`；
2. 父进程实际收到 `JOB_OBJECT_MSG_JOB_MEMORY_LIMIT`；
3. 既有受控 adapter/coordinator 明确抛出 typed memory-budget error。

这是正证据闭集。completion message 缺失不是“未发生超限”的否定证据；如果同一运行已有
peak 或 typed error 正证据，仍按 memory budget 处理。但 message 缺失本身也不是肯定证据。
普通非零退出、stderr 或 `MemoryError` 文本、测试已知的 allocation intent、低于 exact ceiling
但与其接近的 peak，均不得推断为 memory budget error，也不得扫描 child 输出猜测成功。

Windows Job ABI 同时冻结如下：`JobObjectBasicAccountingInformation` 的 information class
literal 必须为 `1`，且只配套 `_WindowsBasicAccounting`；
`JobObjectExtendedLimitInformation` 的 information class literal 必须为 `9`，且只配套
`_WindowsExtendedLimit`。fake ABI test 必须独立写出 literal `1/9` 作为 expected value，不能从
生产常量读取后形成自洽分支；错误 basic class `8` 不得通过 `_WindowsBasicAccounting` 路径。

Windows hard limit 下 child 非零退出而上述正证据全部缺失时，沿用失败所在阶段的既有 closed
错误：main decode/index 分别为 `alignment_main_decode_failed` /
`alignment_main_index_failed`；auxiliary decode/recognition/verification 分别为
`auxiliary_decode_failed` / `auxiliary_recognition_failed` /
`auxiliary_verification_failed`。失败 child 的输出一律不接纳；其他机位已经形成的安全结果继续
遵守第 3 节的 partial delivery，不把局部失败升级为伪造的全局 memory error。不得因此新增错误
code、改变 closed unions、alignment artifact schema 1、tool schema 31 或 Project-media
OperationRecord schema 2，也不得自动重试或提高 ceiling。

后续 Windows 原生实现必须用文档化、确定性的 launch barrier 关闭 target launch 到
`AssignProcessToJobObject` 的归属窗口，使目标命令在成功归属 Job 前不能执行或生成 descendant。
Job 必须同时带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`。cleanup 分成两个互斥的 closed path：

1. **verified normal drain**：timeout、error、interrupt 或 memory-positive-evidence cleanup 开始时，
   只要 root 或任一 descendant 仍活跃，就必须先成功调用 `TerminateJobObject`；然后完成 root
   `Popen.wait`，再用 correct class `1` + `_WindowsBasicAccounting` 查询取得
   `ActiveProcesses == 0`。随后才依次关闭 Job、completion port、initial-thread/gate 等全部长期
   handles；所有 `CloseHandle` 都成功后才释放 operation writer lock。若 root 已自然退出且 class-1
   observation 已为 active zero，可以不调用 terminate。completion message 不能替代 class-1 查询。
   该路径证明完整 child tree 已排空，但不要求生产代码逐个取得或 wait 每个 descendant handle。
2. **kernel-control failure emergency containment**：如果 `TerminateJobObject` 或 correct class `1`
   查询持续失败、因而无法取得 normal-drain postcondition，则允许关闭最后一个仍由 Roughcut
   持有、且带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job handle，以 Win32 的 kill-on-close 语义
   containment；成功关闭该 Job handle 后必须完成 root `Popen.wait`，再逐一关闭 completion port、
   initial-thread/gate 等全部剩余非 Job handles。所有剩余 close 调用均已完成后才释放 writer lock；
   若这些 close 全部成功，释放后返回触发 emergency 的既有 failure；若任一 close 失败，释放后让
   remaining-handle cleanup failure 通过既有 closed failure 边界可见。该路径不得发布 artifact、不得
   写成功，也不得声称在 Job close 前取得 class-1 zero。它不是 normal drain 的替代证据；“持续
   失败”只表示当前 cleanup 无法完成控制/查询，不授权新增重试、PID 扫描或 RSS 近似。

如果 emergency Job `CloseHandle` 自身失败，必须让 cleanup failure 可见；不得被先前 primary error
吞掉，不得声称 containment、安全收口、zero orphan 或 pre-close class-1 zero，也不得返回成功或
发布 artifact。如果 Job close 已成功而后续某个非 Job handle close 失败，该 remaining-handle cleanup
failure 同样必须可见，且不得写 full cleanup success；但也不得反向声称已经发生的 Job
kill-on-close containment 没有发生。所有剩余 close 调用返回后才能释放 writer lock。assignment/
barrier 等更早的失败仍须使目标不能继续执行；公开 error code、closed union、schema 和预算保持
不变。

## 5. Decision 投影与平行输出

### 5.1 可交付输入与 pure prepare

`multicam_parallel_render_prepare` 是纯调用，不写 Project、WorkflowRun、operation、artifact、
staging、日志或媒体。请求是拒绝未知字段的 closed object，字段恰好为
`project_path/edit_version_id/alignment_ref/auxiliary_camera_ids/expected_revision`；
`auxiliary_camera_ids` 非空、唯一并保留请求顺序。`alignment_ref` 必须是 succeeded
`align_multicam` record 的 exact result ref，字段恰好为：

```json
{"kind":"multicam_alignment","alignment_id":"aln_safe-id","schema_version":1,"content_hash":"sha256"}
```

prepare 只接受**当前已采用 Decision**：`edit_version_id` 必须同时等于 Project 的
`active_edit_version_id`、exact Decision 的 ID，以及唯一有效 `adopt_roughcut` ActionReceipt 的
`mutation.artifact_id`；receipt 的 decision mutation、roughcut ApprovalRecord subject、WorkflowRun
decision ref、Project ID 和 content hash 必须形成同一条 exact 链。对应 run 可以仍为 active，
也可以在主 Render 后 completed；canceled run、仅仅 active 但未被 `adopt_roughcut` 采用的 Edit、
历史采用版本或字段相同但 bytes/hash 不同的 Decision 均拒绝。prepare 不修改现有 Decision、
ApprovalRecord、ActionReceipt 或 WorkflowRun schema。

core 以 `alignment_ref.alignment_id` 读取 artifact 后，还必须读取 artifact 的
`producer_operation_id` 所指 record。只有 record 为 schema 2、type=`align_multicam`、
status=`succeeded`、`error=null`，且其 `result_ref` 与请求的 `alignment_ref` **逐字段完全相等**，
artifact producer、ID/schema/content hash 也全部吻合时，alignment 才可交付。published 后
interrupted 的 artifact、failed/pending/running record、空 result、别的 operation 的 result、
或调用方拼出的等值子集都不可 prepare。

每个被选机位还必须满足：alignment camera status 为 `complete|partial`，并且把当前 Decision
投影后 `mapped_ticks > 0`。`omitted`、`failed`、artifact 内零 mapped，或 artifact 虽有 mapped
但当前 Decision 投影为零 mapped 的机位一律不可选择；core 不为它们准备全黑文件。pure prepare
domain error code union 恰好为 `parallel_render_prepare_revision_conflict`、
`parallel_render_decision_not_adopted`、`parallel_render_alignment_not_deliverable`、
`parallel_render_camera_not_deliverable`、`parallel_render_source_stale`。这些错误及 closed-input
解析失败都零写入。平台拆分后，pure prepare public closed union 仅再包含既有
`parallel_render_runtime_unavailable`；该 code 只能由第 6 节 guard 在访问 Project/media/staging
之前返回，不改变上述 prepare application domain union。

### 5.2 Prepare identity 与 closed summary

prepare 逐 clip 按 Decision 顺序投影，并在 alignment partition 边界拆 slot。固定输出 profile
恰好为：

```json
{
  "name":"roughcut_main_h264_aac",
  "version":1,
  "video_encoder":"libx264",
  "video_pixel_format":"yuv420p",
  "video_preset":"medium",
  "video_crf":18,
  "audio_encoder":"aac",
  "audio_channels":2,
  "faststart":true
}
```

`output_settings` 恰好为当前 Project 的
`width/height/frame_rate{numerator,denominator}/audio_sample_rate`；
`output_settings_hash = canonical_sha256_v1({"kind":"multicam_parallel_output_settings",
"schema_version":1,"profile":<exact profile>,"settings":<exact output_settings>})`。
core 构造下列 closed `basis`，所有嵌套 object 都拒绝未知字段，数组顺序有意义：

```json
{
  "kind":"multicam_parallel_prepare_basis",
  "schema_version":1,
  "project":{"project_id":"project_safe-id","revision":1},
  "decision":{"ref":"<exact decision_ref>","adoption":"<exact decision_adoption>"},
  "alignment":{"ref":"<exact alignment_ref>","producer":"<exact alignment_producer>"},
  "auxiliary_camera_ids":["aux-1"],
  "output_profile":"<exact profile>",
  "output_settings":"<exact output_settings>",
  "output_settings_hash":"sha256",
  "total_ticks":0,
  "video_frame_quota":0,
  "audio_sample_quota":0,
  "estimated_temporary_disk_bytes":1,
  "manifest":{"filename":"manifest.json","relative_path":"manifest.json"},
  "cameras":[]
}
```

其中 `decision_ref` 字段恰好为
`kind/edit_version_id/schema_version/content_hash`；`decision_adoption` 恰好为
`run_id/receipt_ref/approval_ref`，两个 ref 分别复用既有 closed
`action_id/receipt_schema_version/receipt_hash` 与
`approval_id/record_schema_version/record_hash`；`alignment_producer` 恰好为
`operation_id/operation_type/result_ref`，type 固定为 `align_multicam` 且 result ref 与
alignment ref exact 相等。`estimated_temporary_disk_bytes` 是 core 根据 exact output profile/settings、
全局 frame/sample quota 与 selected camera count 以同一 schema-1 估算器确定的正整数 planning
estimate；相同 basis 输入必须得到相同值，并被 prepare identity 绑定，Host/Agent 不得改写。
H.264 CRF 输出大小不能在编码前精确预知，因此该字段不是逐字节输出承诺或 staging hard quota；
start 在创建 FFmpeg child 和媒体 staging 前只以它执行当前同文件系统可用空间门，空间不足才使用
`parallel_render_disk_budget_exceeded`。编码中的实际写入失败仍按首个 closed cause 收敛为
`parallel_render_staging_failed`，不得把估算值伪装成已验证的硬上限。

身份派生固定为：

```text
plan_basis_hash = canonical_sha256_v1(<exact basis>)
prepare_id = "mpr_" + first_32_lower_hex(canonical_sha256_v1({
  "kind": "multicam_parallel_prepare",
  "schema_version": 1,
  "plan_basis_hash": plan_basis_hash
}))
```

`prepare_ref` 是拒绝未知字段的 closed object，恰好为：

```json
{
  "schema_version":1,
  "prepare_id":"mpr_32-lowercase-hex",
  "project_id":"project_safe-id",
  "project_revision":1,
  "decision_ref":{"kind":"decision","edit_version_id":"edit_safe-id","schema_version":1,"content_hash":"sha256"},
  "decision_adoption":{"run_id":"run_safe-id","receipt_ref":{"action_id":"action_safe-id","receipt_schema_version":1,"receipt_hash":"sha256"},"approval_ref":{"approval_id":"approval_safe-id","record_schema_version":1,"record_hash":"sha256"}},
  "alignment_ref":{"kind":"multicam_alignment","alignment_id":"aln_safe-id","schema_version":1,"content_hash":"sha256"},
  "alignment_producer":{"operation_id":"op_uuid-v4-hex","operation_type":"align_multicam","result_ref":{"kind":"multicam_alignment","alignment_id":"aln_safe-id","schema_version":1,"content_hash":"sha256"}},
  "auxiliary_camera_ids":["aux-1"],
  "output_settings_hash":"sha256",
  "plan_basis_hash":"sha256"
}
```

成功 response 在既有 common fields 外只含 `prepare_ref/summary`。`summary` 是 closed object，
顶层字段恰好为 `schema_version/prepare_id/plan_basis_hash/project_id/project_revision/decision/
alignment/output_profile/output_settings/output_settings_hash/total_ticks/video_frame_quota/
audio_sample_quota/estimated_temporary_disk_bytes/manifest/cameras`。`summary.schema_version=1`；
`prepare_id/plan_basis_hash` 与 prepare ref 相等；`project_id/project_revision` 分别等于
`basis.project.project_id/revision`；其余 `decision` 至 `cameras` 字段与 basis 的同名字段逐字段
相等。summary 不复制 basis 的 `kind/schema_version/project/auxiliary_camera_ids`；camera 顺序本身
必须与 `prepare_ref.auxiliary_camera_ids` 逐项相等。
用户查看 core 返回的覆盖、黑画/静音、机位、总 ticks、全局 frame/sample quota、临时磁盘估算、
文件名和规格后才可批准 start。Host/Agent 只能原样回传 prepare ref，不能计算、补字段、
逐字段修改或从 summary 重建。

### 5.3 Camera plan、slot 与全局 quota

summary/basis 的每个 camera plan 都是 closed object，字段恰好为
`camera_id/alignment_status/coverage_status/render_status/mapped_ticks/missing_ticks/
uncertain_ticks/conflict_ticks/black_silence_ticks/planned_output/slots`。`alignment_status` 只能为
`complete|partial`；`coverage_status=complete` 当且仅当当前 Decision 的全部 ticks mapped，
否则为 `partial`；`render_status` 在 prepare 中固定为 `planned`。四类 ticks 之和等于
`total_ticks`，`mapped_ticks > 0`，`black_silence_ticks` 等于后三类之和。`planned_output`
恰好为：

```json
{"filename":"camera_aux-1.mp4","relative_path":"camera_aux-1.mp4"}
```

文件名按 exact safe camera ID 形成；`filename == relative_path`，只允许单个 POSIX 普通文件名，
不得含 `/`、`\\`、`.`/`..` 或路径转义。每个 camera 的 slots 非空，按 Decision clip 顺序和该
clip 内 source ticks 递增；每项是拒绝未知字段的 closed object，字段恰好为：

```json
{
  "slot_id":"slot_000000",
  "decision_clip_ref":{"clip_id":"clip_safe-id","clip_ordinal":0,"source_id":"src_main_1","source_start_ticks":0,"source_end_ticks":120000},
  "classification":"mapped|missing|uncertain|conflict",
  "auxiliary_ref":{"source_id":"src_aux_1","source_start_ticks":30000,"source_end_ticks":150000},
  "output_start_ticks":0,
  "output_end_ticks":120000,
  "video_frame_start":0,
  "video_frame_end":25,
  "video_frame_quota":25,
  "audio_sample_start":0,
  "audio_sample_end":48000,
  "audio_sample_quota":48000
}
```

`slot_id` 在每个 camera 内按输出顺序固定为零起点六位十进制 `slot_000000`、
`slot_000001`……；`clip_ordinal` 是当前 Decision 顺序，不是 Source/import 顺序。
`decision_clip_ref.clip_id/clip_ordinal` 必须引用 exact Decision clip，source ID 和 ticks 是该 slot
在该 clip 内的 exact 子区间；同一 clip 的连续 slots 必须无洞、无重叠地覆盖原 Decision clip 的
完整 source range。`mapped` 的 auxiliary ref 非空且与该 source 子区间等长；其余三类必须为
null。source/output tick 必须 `end > start >= 0`；frame/sample 边界必须
`end >= start >= 0`，quota 必须等于差且可为 0。所有 output tick 区间从 0 首尾相接并覆盖
total ticks。

任意全局输出边界 `t` 的 frame/sample 边界只计算一次：

```text
F(t) = round_nonnegative_half_up(t * frame_rate.numerator
                                 / (120000 * frame_rate.denominator))
S(t) = round_nonnegative_half_up(t * audio_sample_rate / 120000)
```

slot 的 start/end 分别等于 `F/S(output_start/end_ticks)`，quota 等于 end-start。不得先量化
Decision clip、alignment partition 或单个 slot 后累加；即使 slot 不是整帧/整 sample，最后一个
slot 的 end 也必须精确等于 summary/global quota 和当前主 Decision Render schedule totals。

### 5.4 Parallel manifest schema 1

published tree 中 manifest 唯一路径为 `manifest.json`。manifest 是递归拒绝未知字段的 closed
object，顶层字段恰好为：

```json
{
  "schema_version":1,
  "parallel_render_id":"mpr_32-lowercase-hex",
  "project_id":"project_safe-id",
  "created_at":"RFC3339 UTC",
  "producer":{"operation_id":"op_uuid-v4-hex","operation_type":"render_multicam_parallel"},
  "prepare_ref":"<exact prepare_ref>",
  "decision":{"ref":"<exact decision_ref>","adoption":"<exact decision_adoption>"},
  "alignment":{"ref":"<exact alignment_ref>","producer":"<exact alignment_producer>"},
  "delivery_status":"complete|partial",
  "output_profile":"<exact profile>",
  "output_settings":"<exact output_settings>",
  "output_settings_hash":"sha256",
  "total_ticks":0,
  "video_frame_quota":0,
  "audio_sample_quota":0,
  "manifest_file":{"filename":"manifest.json","relative_path":"manifest.json","project_relative_path":"renders/multicam/mpr_id/manifest.json"},
  "cameras":[]
}
```

除 `created_at/producer/parallel_render_id/delivery_status/manifest_file` 与实际 render fields 外，
identity、settings、totals 和 cameras plan 必须逐字段复现被 start 接纳的 prepare basis；不得把
Decision、alignment 或 prepare 只记录为松散 ID。manifest cameras 顺序与
`prepare_ref.auxiliary_camera_ids` 相同。每项恰好为
`camera_id/alignment_status/coverage_status/render_status/mapped_ticks/missing_ticks/
uncertain_ticks/conflict_ticks/black_silence_ticks/output/error/slots`。camera/alignment/coverage、
五项 ticks 和 slots 与 summary 对应 camera plan 逐字段相等；prepare 的 `planned_output` 不复制
进 manifest，改由下述 `output/error` 联动表达；`render_status` 从 planned 收敛且只允许
`succeeded|failed`：

- `succeeded` 当且仅当 `output` 非 null 且 `error=null`。output 恰好为
  `filename/relative_path/project_relative_path/bytes/content_hash`；filename/relative path 必须等于
  planned output，project path 必须位于 exact final root，bytes 为正整数，content hash 为 64 位
  小写 SHA-256，并与 strict file readback 相等；
- `failed` 当且仅当 `output=null` 且 `error` 非 null。error 恰好只有 `code`，且 code 为
  `parallel_camera_encode_failed|parallel_camera_verify_failed`；不得保留假文件或空占位文件；
- `delivery_status=complete` 当且仅当所有 selected cameras succeeded；至少一个 succeeded 且
  至少一个 failed 时只能为 `partial`。coverage partial 与 render partial 是两个正交状态；一个
  含黑画/静音但成功验证的文件仍是 render succeeded。

manifest 自身不包含自引用 hash；其 canonical bytes SHA-256 只进入 operation result ref 的
`manifest_content_hash`。existing Render manifest schema 不变。

### 5.5 Start、partial/all-failed 与原子发布

`multicam_parallel_render_start` 请求恰好为 `project_path/operation_id/prepare_ref`，递归拒绝
未知字段。它以新的 operation ID 启动独立 `render_multicam_parallel` Project-media
OperationRecord schema 2；不修改 `approve_export`、主 Render、WorkflowRun 或九个 action。
existing-first 与 status 语义和第 4 节相同。missing operation 才在任何 record/worker/staging
前重新执行 pure prepare；任何 missing/unknown/type 变化、伪造或逐字段修改的 prepare ref，
以及重算后任一 identity/basis 不同，都返回 `parallel_render_prepare_stale` 且零写入。

start 的 `request_hash` 绑定去掉 `project_path` 后的 operation ID 与 exact prepare ref；
`input_hash` 绑定首次重算的 exact basis、全部 mapped Source identity/fingerprint、Decision 和
alignment exact bytes/hash、persistent FFmpeg/ffprobe/runtime identity、output profile/settings、
deterministic temporary-disk estimate 与 global frame/sample quota。固定 phases 是
`parallel_render_preparing/parallel_render_encoding/parallel_render_verifying/
parallel_render_revalidating_basis/parallel_render_publishing`；terminal phase message codes 是
`parallel_render_succeeded/parallel_render_failed/parallel_render_interrupted`。成功 result ref
恰好为 `kind/parallel_render_id/schema_version/manifest_content_hash`，kind 固定为
`multicam_parallel_render`、schema version 固定为 1。

mapped slot 使用对应 auxiliary Source 的原画面和原声；missing/uncertain/conflict 使用黑画面和
数字静音。一个机位失败时，worker 先终止并 wait 该机位 child、删除该机位未验证的临时输出，
再继续其他 selected cameras。**只有至少一个机位 succeeded** 才允许 OperationRecord succeeded
和 final publish：所有 selected cameras succeeded 形成 complete final；成功子集加失败披露形成
一次原子 partial final。若所有 selected cameras 都失败，record 必须 failed，terminal error code
固定为 `parallel_render_all_cameras_failed`，`result_ref=null`，manifest/final publish count 为 0；
不得把“零成功机位”写成 succeeded partial。

`parallel_render_id` 确定为
`mpr_` 加 `canonical_sha256_v1({"kind":"multicam_parallel_render","prepare_ref":<exact>,
"operation_id":<start operation_id>})` 的前 32 个小写 hex。每个 operation 只使用
`<project>/renders/multicam-staging/<operation_id>/`，writer lock 覆盖全部 FFmpeg children。
publish 前对完整 tree 执行普通文件/single-link/no-escape、实际 bytes、hash、slot/global quota 与
strict manifest readback；不得用实际 bytes 与 prepare 磁盘估算不相等拒绝一个已经完整验证的输出。
随后以一次 same-filesystem atomic no-replace directory move 发布到
`<project>/renders/multicam/<parallel_render_id>/`，fsync 父目录并 exact readback。不得逐文件暴露
final，也不得 check-then-rename 覆盖竞争者。

该 move 之后的 atomic-final integrity 仍属于同一个 parallel publish contract。move 前 staging 必须
写入 bounded、closed 的 operation-owned `.publish-intent.json`，只携带 operation ID、
`parallel_render_id` 和 manifest content hash；它随 directory move 进入 final，但不是成功 manifest
或 output schema。succeeded record 及其 exact result ref 是唯一 publication commit：marker 存在、
但 exact operation record 不是 succeeded，或 marker identity、manifest hash、完整 tree 无法验证时，
`read_published_manifest` 必须 fail closed，绝不因 final 目录存在而接纳成功。若 exact succeeded
record 与 marker/hash/tree 全部匹配，reader 将 marker 解释为已提交发布；marker 是 operation-owned
的待清理 read fence，而不是让合法 success 暂时不可读的中间态。worker 先写 succeeded record，再
按 exact result ref 尝试幂等清除 marker；unlink/fsync 失败可留下 fence，但 canonical reader 仍须
可读该已验证 success。`media_operation_status` 的 reconciliation 必须实际调用同一 canonical
reader，持续验证失败则报稳定 integrity error，不扫描 final、mtime 或 latest。

move 后 `_sync` 或 `_validate_final_tree` 失败时，publish 必须先独立验证 final 的 operation/render
identity、expected manifest/hash、完整 expected tree、普通 single-link 文件和 publish intent，再以同
filesystem no-replace move 回该 operation 的 staging，fsync final/staging 父目录并交给既有
`remove_staging`。不能证明 ownership，发现 symlink/hardlink/未知 entry、existing winner 或 rollback/
cleanup 失败时不得删除或覆盖 final；保留 primary `parallel_render_publish_failed`（或原本的
`parallel_render_final_conflict`），前者的 bounded evidence 仍是
`parallel_manifest_publish_failed`，recovery 失败仅使用 `check=manifest_publish_recovery` 与闭合的
expected/actual，不保存 path/exception。final conflict 不产生伪 recovery evidence。

因此 operation 只有在 exact final tree 验证完成且 succeeded record 的 exact result ref 成立时才是
成功；marker 的物理清除不是第二个 success truth。caught post-move failure 写 failed/null result；
硬退出不被 recovery catch，记录会按现有规则收敛 interrupted，marker 在没有 exact succeeded record
时让 canonical final reader 拒绝。相同 operation/request 只读既有 record 或做确定性 reconciliation，
不 rerender、不覆盖、不创建第二结果；MediaOperationStore 仍是唯一 operation truth，
MulticamParallelStore 仍是唯一 output store，正常 success tree、manifest 和 result identity 不变。

ordinary、仍受 worker 控制的全局失败和 all-failed 必须在 writer lock 内 terminate+wait children，
并由**该 worker**作为唯一 cleanup actor，在写 terminal failed record 前尝试删除自己的 operation
staging 并验证不存在。若 staging 创建、受控写入或 partial publish 前清除失败机位临时文件本身是
首个 terminal cause，使用 `parallel_render_staging_failed`、零 final。若 all-failed、basis/runtime
change 或其他更早的 closed terminal cause 已确定，后续 cleanup 失败不得覆盖该 code；staging 可
保留为未接纳证据，尤其 all-failed 仍必须 `parallel_render_all_cameras_failed`、零 final。纯
`media_operation_status` 在任何状态都不扫描 final、不接纳结果、也不清理 staging；因此 ordinary
worker cleanup 与 status 的 no-cleanup 没有重叠责任。publish 前 hard exit 的 worker 无法完成
ordinary cleanup，record 收敛 interrupted 且 staging 保留；directory move 后、record succeeded
前 hard exit 时 final 可完整存在但 record 仍 interrupted，status 不接纳或清理。record succeeded
后响应丢失只 exact readback，零 worker/零再发布。

旧 interrupted operation 的完整 final 只属于旧 operation。显式重跑必须重新批准并使用新
operation ID，派生新 final；新 worker 不扫描、复用、删除或覆盖旧 final。
`parallel_render_revalidating_basis` 在 publish 前重验 mapped Sources、current adopted Decision、
alignment exact ref/bytes/producer、output settings 和 runtime。前四类变化为
`parallel_render_basis_changed_during_run`，runtime 变化为
`parallel_render_runtime_changed_during_run`；均为 operation failed、worker ordinary cleanup、零
final。final no-replace 竞争为 `parallel_render_final_conflict`。

### 5.6 Closed `render_multicam_parallel` OperationRecord error mapping

本节只关闭 schema-2 `render_multicam_parallel`，不泛化其他 OperationRecord。它新增的 failure
action union 恰好为 `validate_parallel_render_basis/manage_parallel_render_staging/
render_parallel_cameras/revalidate_parallel_render_basis/publish_parallel_render`。failed record 的
`message_code` 固定为 `parallel_render_failed`，closed mapping 恰好为：

| terminal `error.code` | `responsibility` | `action` | `message_code` |
|---|---|---|---|
| `parallel_render_disk_budget_exceeded` | `roughcut_core` | `validate_parallel_render_basis` | `parallel_render_failed` |
| `parallel_render_all_cameras_failed` | `roughcut_core` | `render_parallel_cameras` | `parallel_render_failed` |
| `parallel_render_basis_changed_during_run` | `roughcut_core` | `revalidate_parallel_render_basis` | `parallel_render_failed` |
| `parallel_render_runtime_changed_during_run` | `roughcut_core` | `revalidate_parallel_render_basis` | `parallel_render_failed` |
| `parallel_render_staging_failed` | `roughcut_core` | `manage_parallel_render_staging` | `parallel_render_failed` |
| `parallel_render_final_conflict` | `roughcut_core` | `publish_parallel_render` | `parallel_render_failed` |
| `parallel_render_publish_failed` | `roughcut_core` | `publish_parallel_render` | `parallel_render_failed` |

interrupted record 只允许以下三组：

| `error.code` | `responsibility` | `action` | `message_code` |
|---|---|---|---|
| `parallel_render_interrupted` | `host` | `interrupt_media_operation` | `parallel_render_interrupted` |
| `parallel_render_interrupted` | `user_input` | `interrupt_media_operation` | `parallel_render_interrupted` |
| `parallel_render_interrupted` | `roughcut_core` | `recover_abandoned_media_operation` | `parallel_render_interrupted` |

succeeded record 必须 `result_ref` 非 null 且 `error=null`；failed/interrupted 必须
`result_ref=null` 且 error 非 null；pending/running 两者都为 null。parallel per-camera error union
仍恰好为 `parallel_camera_encode_failed/parallel_camera_verify_failed`，只存在于 partial manifest，
不伪装为 OperationRecord error。operation terminal error union 恰好为上表八个 code；
start/preflight closed error union 恰好为 `operation_input_conflict/parallel_render_prepare_stale/
parallel_render_runtime_unavailable/parallel_render_source_stale`，且零 record/worker/staging。

### 5.7 E8 durable bounded parallel failure evidence

schema-2 `render_multicam_parallel` 的 terminal `error` 可以带一个可选的 closed
`evidence` 对象；没有该字段的既有记录按原 schema 读取，其他 operation type 不能携带它。
该对象固定为：

```json
{
  "code": "parallel_camera_verify_failed",
  "camera_id": "aux_1",
  "check": "aac_timeline_quota",
  "expected": 22439520,
  "actual": 22438912,
  "delta": -608,
  "tolerance": 1024,
  "return_code": null,
  "stderr_tail": null
}
```

`code` 只允许 `parallel_camera_encode_failed`、`parallel_camera_verify_failed`、
`parallel_manifest_publish_failed` 或 `parallel_failure_evidence_unavailable`。
encode evidence 固定带 stable `camera_id`、`check=ffmpeg_encode`、`return_code` 和
`stderr_tail`；verify evidence 固定区分 `video_frame_quota`、`aac_timeline_quota` 与
`aac_decoded_quota` 等既有检查。quota 的 `delta` 冻结为 `actual - expected`，video
frame 的 tolerance 为 `0`，AAC timeline/decoded 的 tolerance 继续为 `1024`；因此
`1024` 以内通过，超过才产生失败 evidence。quota `expected/actual/tolerance` 都必须是
`0..9007199254740991` 的 JSON-safe 有界整数，`delta` 固定为
`actual - expected` 且范围为 `-9007199254740991..9007199254740991`；encode
`return_code` 仍是独立的 signed-32-bit 值。manifest publish evidence 不绑定 camera，不伪造无法
取得的 camera 事实。

encode `stderr_tail` 先以 UTF-8 replacement 解码、规范化换行、去除控制字符，再取最后
最多 8 行并裁到最多 2048 个 UTF-8 bytes；secret/token/authorization 值和绝对路径在此边界
脱敏。含空格的 POSIX/drive/UNC 路径按整行保守替换；lone surrogate 先做 UTF-8 replacement，
尾截断只做一次 UTF-8 编码并保留合法字节边界。不会持久化完整 stdout/stderr、command、媒体、
staging/TMPDIR 或用户路径。evidence
序列化/写入失败时保留原 terminal failure code，并只回退到固定的
`parallel_failure_evidence_unavailable`，不能转成 success。

worker 在 ordinary staging cleanup 后写 terminal OperationRecord，因此同一个
`operation_id` 通过 `MediaOperationStore`/`media_operation_status` 在 cleanup、进程退出和
重启后得到相同 closed evidence；纯 status 不清理 staging。partial success 仍保持
OperationRecord `status=succeeded,error=null`，其已发布 manifest 的失败 camera error 保持
既有精确的 `{ "code": "parallel_camera_encode_failed|parallel_camera_verify_failed" }` 形状；
同时，`result_ref` 可在同一 status/readback seam 直接带一个可选的
`partial_failure_evidence`，只保存 planned order 中 first failed camera 的 encode、verify 或
固定 fallback evidence，不保存 publish evidence，也不建立 list/第二份 state。complete success
和无该字段的 legacy 四字段 result ref 都保持原精确形状。worker 仍只收集 deterministic first
failed camera evidence；all-failed 进入 terminal `error.evidence`，partial success 进入上述
result ref observation。

## 6. 历史公开 tool schema 26（readback/forensic）

本节保留 Phase 3 的历史公开面、命令清单和验收文字，仅用于旧请求/旧记录的
readback 与 forensic 对照；schema 29 是历史 additive contract state，当前生产
`TOOL_SCHEMA_VERSION` 为 31，当前 Correlation route 见第 9 节。

schema 26 在 schema 25 全部入口之外只增加：

| CLI | MCP | 类型 |
|---|---|---|
| `roughcut align-multicam ... --json` | `align_multicam` | tracked long start，第 4 节 closed request |
| `roughcut multicam-parallel-render-prepare ... --json` | `multicam_parallel_render_prepare` | pure prepare，第 5.1 节 closed request |
| `roughcut multicam-parallel-render-start ... --json` | `multicam_parallel_render_start` | tracked long start，第 5.2 节 closed request |

三个入口的公共调度顺序固定为：先由 CLI parser/MCP argument decoder 完成既有 closed request
shape/type 校验，再调用同一个 core-owned
`require_m2_7_public_capability(entry_name)`，最后才可进入对应 application。Windows guard 的 exact
映射为 `align_multicam → alignment_runtime_unavailable`、
`multicam_parallel_render_prepare → parallel_render_runtime_unavailable`、
`multicam_parallel_render_start → parallel_render_runtime_unavailable`。guard 不读取 `project_path`，
不探测媒体或 staging，不创建 operation/worker，并且没有环境变量或测试专用生产旁路。

`media-operation-status`/`media_operation_status` 原样复用，不增加第二个通用 status。两个 start
成功 envelope 只增加 `media_operation/operation_readback`，并分别增加 nullable
`alignment` 或 `parallel_render` result；prepare 成功只增加 `prepare_ref/summary`。所有错误
沿用只含 stable code 的标准 envelope，不泄漏路径、文件名、stderr 或命令。

九个 workflow action/input 逐字不变，也不增加第十个 action。普通用户交互仍只有：
素材分组/授权、对轨覆盖摘要、平行输出摘要与明确批准。Review UI 不增加对轨编辑面。

canonical Skill ownership 同时冻结如下：`roughcut` 只路由；`roughcut-basics` 只提供共享安装、
状态和隐私话术；`create-roughcut` 只收集并回读用户精确授权的主/副机位分组，并在当前主 Decision
已 exact adopt 且仍符合用户授权后原样调用 core 的 alignment start/status；分组确认本身不得立即
启动对轨，也不得绕过可跳过的可选分支。`revise-roughcut` 仍只编辑主线 Draft/Decision，不产生任何多机位
判断；`render-roughcut` 只展示 core 返回的 alignment/prepare summary、收集用户选择与一次明确
批准，并原样回传 exact refs/新 operation ID。机位可交付性、当前 adopted Decision、slot、coverage、
quota、路径、hash、partial/all-failed、stale 和 error code 全部只由 Python core 决定，Skill 不得
复制判断或从字段自行推导。`docs/agent-tool-contract.md` 是五份 canonical Skill
`references/tool-contract.md` 的唯一 source；schema 26 公开面变更必须同步后保持 5/5
逐字节一致。

## 8. 止损线

以下任一情况出现即停止进入实现并回到独立架构评审：需要修改主 Project/Decision/Render
schema；需要副机位 ASR、机器推断的场景组/同步组、匹配图、漂移/DTW/schema 2 时间变换；需要第二 installer/
runtime manager；需要 queue/daemon/retry/worker recovery；需要绕过 pure prepare 直接启动正式
平行 Render；或必须猜测 missing/uncertain/conflict 才能获得可接受覆盖。用户明确选择的
synchronization group 与 exact source pairs 是已批准的输入边界，不属于机器推断。

## 9. 当前用户已明确选择同步组后的 Correlation 生产对轨（Core 0.2.8）

本节是当前生产 writer 的规范覆盖。`align_multicam` 新写入只接受 persistent managed
Audalign 1.3.1 selection，并固定 algorithm `audalign_correlation`、profile
`roughcut_audalign_correlation_fixed_offset` v1、evidence family
`roughcut_audalign_correlation_evidence_v1` 和 upstream commit
`d5955ae8a85b1cd480dadd005c3f88986f4ebbef`。BBC、旧 Audalign Fingerprint/profile 1/2、
waveform artifact/request 只按 exact ID/hash readback；BBC selection 在不存在历史 artifact
时直接 fail closed，零 worker、零 artifact。

### 9.1 同步组与 exact-pair 边界

- `main_camera` 与 `auxiliary_cameras` 是用户明确选择的同步组。提供 `source_pairs` 时，
  每项恰为 `main_source_id/auxiliary_source_id`，两端必须属于对应组，重复 pair、unknown
  field、unknown source 和空 pair 都拒绝；Core 只按排序后的显式 pairs 执行，不做 Cartesian、
  filename、index、count 或 matching-graph 推断。
- 未提供 pairs 时，只接受恰好一个 main Source 与一个 auxiliary Source 的无歧义 pair；
  多文件组必须 fail closed。声明组内未配对 Source 只做存在性闭包检查，不进入 fingerprint、
  decode、probe、worker、input hash source basis 或 publish revalidation。
- 每个 unique main Source full decode 一次；每个 exact pair 的 auxiliary 只产生一个 bounded
  15 秒 excerpt。所有 child 受 operation time、memory、workspace/TMPDIR 和 identity snapshot
  约束；source identity/fingerprint/runtime drift 在 publish 前一律 operation failed、零发布。

### 9.2 Correlation 执行与资源边界

- FFmpeg 输出必须是 44.1 kHz、mono、PCM16 LE WAV；aux excerpt 必须恰好 `661500` frames。
  FFmpeg non-zero、不可读、wrong stream/channels/rate/width/length 都是 pair-level uncertain
  `auxiliary_decode_failed`，其余安全 pair 可继续；time/memory/disk/operation deadline 是
  operation-level failure，workspace cleanup 后必须 recheck，不能吞掉预算错误。
- probe start 是 auxiliary duration ticks 的 20%、50%、80% floor 后 clamp 到
  `[0, duration - 1_800_000]`；短 source 或 clamp duplicate 直接 `insufficient_probes`/
  `uncertain`。三次严格串行，`target=aux excerpt`、`against=full main`，且
  `B = 0 - auxiliary_probe_start + Decimal seconds_to_ticks(delta)`，其中 `delta` 先按
  worker 输出的十进制文本做 ties-away-from-zero 整数 ticks 换算。
- CorrelationRecognizer 只使用 pinned wheel 的 canonical defaults：sample rate 8000、FFT
  4096、filter 0.0、freq 200、normalize true、locality/max_lags/match filters/start_end/
  start_end_against/passthrough 按 wheel 默认值、fail-on-decode true、固定 read/write extension
  lists、overlap/locality ratios、SCALING_16_BIT 65536。worker 接收 parent 生成的 typed config
  并逐字段核对；不传入 overrides，不做 preprocess/noisereduce/torch、L/R second pass、
  waveform/BBC/Fingerprint fallback 或 confidence/score admission。

### 9.3 Admission、evidence 与 timeline

- 每个 probe 最多贡献一个 offset。有效 B 必须由至少两个不同 probe 组成唯一 non-chaining
  cluster，diameter/spread `<=12000 ticks`；chaining、多个 qualifying clusters、无 candidate、
  no-overlap、malformed response 或任何 worker failure 都不得 mapped。worker failure 的 exact
  pair 即使另外两个 probe 一致也保持 `uncertain`/`worker_failed`。
- mapped evidence 必须保存 requested provider/recognizer/profile fields、完整三 probe
  records、support count（>=2）、spread（<=12000）、representative（属于 winning cluster 且
  是 20→50→80 中首个成员）和 `conflicting_b_ticks=[]`。uncertain evidence 的 support、spread、
  representative、conflicts 必须为空/零，并只使用 closed codes
  `insufficient_probes/no_candidate/probe_inconsistent/auxiliary_decode_failed/worker_failed`；Correlation evidence
  不得出现 Fingerprint counts/confidence，Fingerprint evidence 也不得伪装成 Correlation。
- 同一 timeline 的 Correlation owners 在 `<=12000 ticks` 内按 deterministic source ID 选一个
  owner；若差异超过该阈值，生成 `fixed_offset_conflict` 与 `conflicting_b_ticks`。未知、mixed
  或缺失 profile family 都是 `alignment_integrity_error`，BBC/waveform historical family 的
  readback 分派保持原语义。artifact 的 `representative_b_ticks` 可见，区间只保存等速整数 ticks。

### 9.4 Operation、发布与当前状态

- existing-first exact-ID readback 在安全加载 current Project identity/scope 后、任何 Source、runtime 或 budget
  validation 之前发生；新写入只由 `run_align_multicam` 进入 Correlation executor，不能通过 fake
  path、BBC writer/executor 或旧 fallback 分支切换。publish 使用 immutable artifact、atomic
  no-replace 和 exact producer/result-ref readback。
- current provider 只在 persistent managed runtime closure、Python 3.11、absolute regular
  non-symlink executable、distribution/lock/license/manifest receipts 和 platform validation
  全部通过时为 `ready`；diagnostics 对有效 Audalign 返回 `available/production_ready=true`，
  BBC 返回 `provider_mismatch`，不使用 BBC error 类表达 Audalign 错误。
- 本轮 synthetic adapter/coordinator/fixture 证据覆盖 closed response、exact pairs、serial
  probes、bounded workspace、failure cleanup、identity drift 和 historical readback；未读取
  或修改真实素材、未执行 Mac 安装/UX/长素材回归。该未验证事实不改变历史 forensic 记录。
