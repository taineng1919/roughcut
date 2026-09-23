# Project Media OperationRecord Contract


适用操作：`transcribe_source`、`proxy_create` 和 `approve_export` 的正式 Render。

配套 vectors：`core/tests/fixtures/media-operation-record-vectors.json`

## 1. 边界与既有真相源

本契约只为三种既有同步媒体任务保存可信的运行状态。它不保存媒体正文、不排队、不自动
重试、不续算、不恢复 worker、不提供通用 cancel、不保存 PID、不伪造百分比，也不创建
第四种 operation type。

媒体 OperationRecord 是独立的 Project 内 schema 1，不继承或泛化 installation
OperationRecord。实现可以复用已经验证的 canonical JSON、SHA-256、原子文件写入和
跨平台瞬时锁原语，但必须使用单独的 closed value/store/application；不得增加基类注册表、
动态 payload、任务 DSL 或通用 job framework。

既有真相源保持不变：

- ASR：raw ASR 是诊断证据；Timed Transcript 是不可变正文；Project
  `active_transcript_versions` 是活动版本；Workflow binding synchronizer 只同步 exact
  Transcript。
- Proxy：`ProxyManifest`、固定 cache key 和受控 MP4 是 ready 真相；OperationRecord
  不成为第二份 cache manifest。
- Render：export claim 只负责并发互斥，closed `owner.json` 只负责 staging 身份，
  Workflow `TransactionMarker`/ActionReceipt/ApprovalRecord/WorkflowRun 负责正式发布和
  业务门。OperationRecord 不替代其中任何一个。
- Draft Workspace Checkpoint 仍只服务 `draft_review` 指针/undo/redo，与媒体 operation
  无读写关系。

OperationRecord 状态变化本身不得修改 Project revision、WorkflowRun stage/lifecycle、
approval、receipt、checkpoint 或 artifact。既有领域提交可以按原规则修改 Project 或
WorkflowRun；record 只在那些提交完成后保存 exact result ref。

## 2. 固定路径、scope 与锁

三种 record 共用一个 Project 内受控目录，但不共用 installation store：

```text
<project>/workflow/operations/media/<operation-id>.json
<project>/workflow/operations/media/.<operation-id>.json.tmp
<project>/workflow/operations/media/.<operation-id>.writer.lock
```

调用方只提供 safe operation ID，不能提供 record、temp 或 lock 路径。operation ID 复用
workflow `validate_safe_id`，必须匹配 `[A-Za-z0-9_-]{1,128}`；它是非秘密幂等键，不是
bearer token。Project root 使用 lexical absolute identity；Project
root、`workflow`、`operations`、`media` 和所有现存 record/temp/lock 节点逐级 `lstat`。
目录不得为 symlink；文件必须是普通文件且只有一个 hard link；containment 或身份不明时
fail closed。旧 Project 缺少 `operations/media` 时，普通读取返回 not found 且不创建
目录；第一次合法媒体 operation 才创建受控目录，不修改 `project.json`。

`scope` 恰好为：

```json
{
  "kind": "project",
  "project_id": "project_fixture",
  "project_root_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`project_root_hash` 由 core 对 lexical canonical Project root 做 canonical JSON v1
SHA-256；路径不出现在 record、日志或响应。复制到另一 Project、project ID/path/scope
不一致一律 `operation_integrity_error`。

每个 operation ID 有一个 non-blocking、同进程安全的瞬时 writer lock：POSIX 使用
`flock`，Windows 使用 `msvcrt.locking`。它只证明持锁的 core/transport worker 是否仍
活着，不单独证明该 worker 启动的媒体子进程已经停止；它不是 PID、lease、心跳或
scheduler。媒体 adapter 开始前必须已原子发布 pending record并持锁；所有仍可能修改
本 operation staging 的 core-owned media child 已停止且不能继续写入、terminal record
落盘后，才释放 operation 的 writer 生命周期所有权。

record 使用 fixed temp、file fsync、strict readback、atomic replace 和 parent directory
fsync。写失败时旧 record 保持权威，不得报告新状态成功。status 在同一 writer lock 内
重读，并且只按第 6 节的媒体子进程收敛条件处理 abandoned record。

以下锁顺序只适用于第 6 节确认 record 不存在后的新任务路径；existing-operation
readback 只验证 scope/ID/request并读取 media record，不取得 export claim 或当前业务
Project/workflow preflight lock。新任务锁顺序手写固定：

1. ASR：既有 Project/public-write lock → media writer lock；status 只取 media writer，
   不反向取得 Project lock。
2. Proxy：media writer lock；Project/Source 使用现有 snapshot revalidation，cache publish
   仍由 deterministic target/atomic pair 规则裁决，不新增 per-source scheduler。
3. Render 使用非嵌套 preflight/publish 顺序：先取得既有 export claim；在
   Project/workflow write lock 内恢复既有 transaction、验证批准并 pure prepare exact
   Render Plan；释放 Project/workflow lock 后才取得 media writer，计算 closed input并发布
   pending；随后执行既有 owner/staging/render；保持 media writer，在正式发布时重新取得
   Project/workflow lock，复验完整 basis并发布既有 TransactionMarker transaction；写
   terminal record 后释放 media writer，最后释放 export claim。

Render 的前置 Project/workflow lock 与 media writer 不同时持有，因此不存在
Project/workflow lock → media writer 与正式发布阶段 media writer →
Project/workflow lock 的反向嵌套。既有 export claim、closed owner、staging 和
TransactionMarker 的内部创建、发布、恢复与清理顺序全部不变。

operation ID 来源固定：

- ASR/Proxy 的内部第二阶段 B coordinator 显式接收调用方稳定 ID；第三阶段公开接线必须让
  Host 在启动同步任务前持有该 ID，不允许任务结束后才生成。
- Render 不增加 frozen workflow action 字段，直接复用 `workflow_action.action_id` 作为
  operation ID；同 action ID/workflow action input 因而同时约束 workflow receipt 与
  Render operation request identity。
- 三种手写 coordinator 分开实现；不得增加接受任意 type/payload 的 generic start。

## 3. closed record schema 1

顶层字段必须恰好如下：

```json
{
  "schema_version": 1,
  "operation_id": "op_0123456789abcdef0123456789abcdef",
  "scope": {
    "kind": "project",
    "project_id": "project_fixture",
    "project_root_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "operation_type": "transcribe_source",
  "request_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "input_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "status": "running",
  "phase_message_code": "transcription_running_asr",
  "created_at": "2026-07-29T09:00:00.000000Z",
  "started_at": "2026-07-29T09:00:01.000000Z",
  "updated_at": "2026-07-29T09:00:02.000000Z",
  "finished_at": null,
  "result_ref": null,
  "error": null
}
```

规则：

- `operation_type` 只允许 `transcribe_source`、`proxy_create`、`approve_export`。
- `operation_id/scope/operation_type/request_hash/input_hash/created_at` 创建后不可改变。
- `request_hash` 与 `input_hash` 都必须是 64 位小写 SHA-256，由 Roughcut core 按
  `canonical_json_v1` 计算；Host、Agent、Skill 不能提交、计算或覆盖。
- 状态只允许 `pending → running → succeeded|failed|interrupted`；terminal 不可回退。
- pending 的 started/finished 为 null；running 的 started 非 null、finished 为 null；
  terminal 的 started/finished 非 null，且 `finished_at=updated_at`。
- 只有 succeeded 有 `result_ref`；只有 failed/interrupted 有 `error`。
- parser 必须交叉校验 type：`transcribe_source→transcription_*→transcript`、
  `proxy_create→proxy_*→proxy`、`approve_export→render_*→render`；混用 phase、result、
  responsibility action 或 terminal message code 一律 integrity error。
- JSON 与 hash 复用 `canonical_json_v1`：UTF-8、Unicode NFC、CRLF/CR→LF、key 排序、
  安全整数和 SHA-256；拒绝 duplicate key、float、BOM、未知字段和未知 schema。
- `request_hash` 缺失、非字符串、非 64 位小写 hex 或任何 closed 字段不符都必须
  `operation_integrity_error`，不得退回旧的仅 `input_hash` 解释。
- record hash 是 canonical record JSON 的 SHA-256，但不额外持久化 hash/state 文件。

## 4. stable request 与完整 execution input identity

`request_hash` 与 `input_hash` 分工固定：

- `request_hash` 只绑定可由同一次公开业务请求稳定重建的 closed request projection，
  专用于相同 operation ID 的幂等判断。它不包含当前 Source snapshot、runtime/model/tool
  selection、current Project after image、current WorkflowRun record hash、Decision 或
  其他成功后可能变化的派生状态。
- `input_hash` 绑定第一次实际执行时经过 preflight 的完整 Source/runtime/model/tool/export
  basis，只作审计和故障证据。existing-operation retry 和纯 status 都不重建或比较历史
  input projection。

两者都只由 core 计算。record 只持久化两个 hash，不增加第三种持久状态或 request payload
sidecar。绝对路径、token、原始 locator、命令行和媒体正文不得进入 projection 或响应。
下列 request projection 示例就是各自的完整 closed 字段集；缺字段、额外字段、错误类型、
duplicate key 或未知 `request_schema_version` 在 hash 计算前拒绝。

### 4.1 三种 closed request projection

#### 4.1.1 `transcribe_source`

```json
{
  "request_schema_version": 1,
  "operation_type": "transcribe_source",
  "scope": {
    "kind": "project",
    "project_id": "project_fixture",
    "project_root_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "source_id": "source_a",
  "expected_project_revision": 7,
  "transcription_request": {
    "speaker_diarization": true,
    "timeout_milliseconds": 3600000
  }
}
```

`speaker_diarization` 是公开请求的用户选择；`timeout_milliseconds` 是公开请求经 core
closed normalization 后的 effective timeout（当前未显式提供时固定为 `3600000`）。关闭
speaker 时仍保存 `false`，但不保存 speaker model/mode；后者属于完整 execution input。
正式 tracked operation 只使用持久 runtime binding，旧的显式 runtime/model path override
不进入 request projection，也不能被改写成 hash 身份。

#### 4.1.2 `proxy_create`

```json
{
  "request_schema_version": 1,
  "operation_type": "proxy_create",
  "scope": {
    "kind": "project",
    "project_id": "project_fixture",
    "project_root_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "source_id": "source_a",
  "expected_project_revision": 7
}
```

当前公开 `proxy_create` 没有 profile selector，因此 request projection 不虚构 profile
字段；derived `ProxyProfile`、cache key、Project settings 和 tool identity 全部只进入
完整 `input_hash`。未来若公开 contract 真正增加 profile 选择，必须升级
`request_schema_version` 并把 exact closed public fields 纳入 projection。

#### 4.1.3 `approve_export`

```json
{
  "request_schema_version": 1,
  "operation_type": "approve_export",
  "scope": {
    "kind": "project",
    "project_id": "project_fixture",
    "project_root_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "run_id": "run_fixture",
  "action_id": "action_export_fixture",
  "workflow_action": {
    "action": "approve_export",
    "input_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
}
```

`workflow_action.input_hash` 是 core 对冻结 `ApproveExportInputV1` 的 canonical hash；
Host/Agent 只提交 closed action input，不提交该 hash。request projection 不包含 current
Decision、export dependency/basis、Prepared Render Plan 或 Run record hash；这些只属于
第一次执行的完整 input。

### 4.2 `transcribe_source` 完整 input

```json
{
  "input_schema_version": 1,
  "operation_type": "transcribe_source",
  "project_id": "project_fixture",
  "source_ref": {
    "source_id": "source_a",
    "source_snapshot_hash": "1111111111111111111111111111111111111111111111111111111111111111"
  },
  "expected_project_revision": 7,
  "transcription_config": {
    "config_schema_version": 1,
    "runtime_binding_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
    "python_receipt_hash": "3333333333333333333333333333333333333333333333333333333333333333",
    "model_refs": {
      "asr": "4444444444444444444444444444444444444444444444444444444444444444",
      "vad": "5555555555555555555555555555555555555555555555555555555555555555",
      "punc": "6666666666666666666666666666666666666666666666666666666666666666",
      "speaker": "7777777777777777777777777777777777777777777777777777777777777777"
    },
    "ffmpeg_tool_selection_hash": "8888888888888888888888888888888888888888888888888888888888888888",
    "speaker_diarization": true,
    "speaker_mode": "punc_segment",
    "timeout_milliseconds": 3600000
  }
}
```

- `source_snapshot_hash` 绑定当前 Project 中 exact Source snapshot；不保存 locator。
  hash 输入是现有 `SourceAsset.to_dict()` 的完整 closed snapshot；record 只保存最终
  input hash，不回显包含 locator 的中间 projection。
- `runtime_binding_sha256` 是已验证持久 `runtime.json` 精确文件字节的 SHA-256；
  `python_receipt_hash` 是 `RuntimePython.receipt` 的 canonical JSON v1 SHA-256；
  `model_refs.asr/vad/punc` 是各 `RuntimeComponent.receipt.value` 中已经验证的 SHA-256
  value；`ffmpeg_tool_selection_hash` 是 `RuntimeTool.to_dict()` 的 canonical JSON v1
  SHA-256。它不是 receipt hash，也不回显其中的绝对 command。
- speaker 关闭时 `model_refs.speaker=null`、`speaker_mode=null`；开启时二者必须非 null，
  `model_refs.speaker` 必须是 CAM++ `RuntimeComponent.receipt.value`，且 public workflow
  hard gate 已验证 exact source 的 transcribe/speaker 授权。
- timeout 用正整数毫秒，禁止 float；其他固定 FunASR 参数属于 component/runtime receipt，
  不接受任意参数字典。
- 正式任务只接受上述已验证持久 runtime binding；本契约不把临时环境变量或未验证显式
  路径扩张为兼容输入。

### 4.3 `proxy_create` 完整 input

```json
{
  "input_schema_version": 1,
  "operation_type": "proxy_create",
  "project_id": "project_fixture",
  "source_ref": {
    "source_id": "source_a",
    "source_snapshot_hash": "1111111111111111111111111111111111111111111111111111111111111111"
  },
  "expected_project_revision": 7,
  "cache_key": "9999999999999999999999999999999999999999999999999999999999999999",
  "profile": {
    "schema_version": 1,
    "profile_version": 1,
    "canvas": {
      "width": 1280,
      "height": 720
    },
    "frame_rate": {
      "numerator": 25,
      "denominator": 1
    },
    "gop_frames": 50,
    "has_video": true,
    "has_audio": true,
    "container": "mp4",
    "video_codec": "libx264",
    "pixel_format": "yuv420p",
    "crf": 23,
    "preset": "veryfast",
    "faststart": true,
    "audio_codec": "aac",
    "audio_sample_rate": 48000
  },
  "tool_refs": {
    "runtime_binding_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
    "ffmpeg_tool_selection_hash": "8888888888888888888888888888888888888888888888888888888888888888",
    "ffprobe_tool_selection_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

profile 必须逐字段等于现有 `ProxyProfile.to_dict()`，cache key 必须等于现有
`proxy_cache_key(source fingerprint, source probe, profile)`；不得新增第二套 cache/hash
算法。两个 `tool_selection_hash` 分别是已验证持久 runtime binding 中 FFmpeg/ffprobe
`RuntimeTool.to_dict()` 的 canonical JSON v1 SHA-256；不得称为 receipt hash或回显绝对
command。Proxy 不要求 active WorkflowRun，也不修改 Project revision，首版也不写
WorkflowRun readiness。

### 4.4 `approve_export` 完整 input

```json
{
  "input_schema_version": 1,
  "operation_type": "approve_export",
  "project_id": "project_fixture",
  "run_id": "run_fixture",
  "approve_export_action": {
    "action_id": "action_export_fixture",
    "input_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  },
  "roughcut_approval_receipt_ref": {
    "action_id": "action_adopt_fixture",
    "receipt_schema_version": 1,
    "receipt_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "decision_ref": {
    "artifact_id": "decision_fixture",
    "schema_version": 2,
    "content_hash": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  },
  "expected_project_revision": 7,
  "export_dependency_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "export_basis_hash": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
  "render_plan_ref": {
    "artifact_id": "render_fixture",
    "schema_version": 2,
    "content_hash": "abababababababababababababababababababababababababababababababab"
  }
}
```

该 projection 在 export claim 内、前置 Project/workflow lock 的 pure prepare 完成且该锁
已经释放后，由 core 在取得 media writer 时构造：

- `run_id`、immutable Decision、roughcut approval receipt、approve_export action input
  hash、`export_dependency_hash` 和 `export_basis_hash` 共同绑定启动时已验证的
  `export_review` basis；不得放入成功后必然变化的当前 WorkflowRun record hash；
- Render Plan 由既有 pure prepare 在内存中生成，ID、时间、输出设置、source bindings、
  clips 和 tool refs 全部已冻结，`render_plan_ref.content_hash` 使用既有 Render Plan
  canonical hash；
- `render_plan_ref.schema_version` 必须来自 actual Prepared plan：单素材 `RenderPlan`
  恰为 schema 1，多素材 `MultiSourceRenderPlan` 恰为 schema 2；不得根据 Decision 猜测，
  也不得新增 Render schema；
- `export_dependency_hash` 和 `export_basis_hash` 复用有限 workflow 的 exact frozen
  dependency/basis，不引入第二套批准；
- caller 不提交输出路径、FFmpeg 参数、Proxy 或 generated Render ID。

`input_hash` 只记录第一次执行时上述完整 basis。响应丢失重试只按第 4.1.3 节的 stable
request projection重算 `request_hash`，不重建 Decision、roughcut receipt、Prepared plan
或当前 WorkflowRun record hash。ActionReceipt/TransactionMarker 继续只承担既有 workflow
transaction 恢复，不新增历史 input sidecar。

## 5. 固定 phase、result 与 error

### 5.1 phase message code

| operation | pending/running（按此顺序） | terminal |
| --- | --- | --- |
| `transcribe_source` | `transcription_preparing`、`transcription_decoding_audio`、`transcription_running_asr`、`transcription_normalizing`、`transcription_publishing_transcript`、`transcription_synchronizing_binding` | `transcription_succeeded` / `transcription_failed` / `transcription_interrupted` |
| `proxy_create` | `proxy_preparing`、`proxy_encoding`、`proxy_verifying`、`proxy_publishing` | `proxy_succeeded` / `proxy_failed` / `proxy_interrupted` |
| `approve_export` | `render_preparing`、`render_encoding`、`render_verifying`、`render_revalidating_basis`、`render_publishing_workflow` | `render_succeeded` / `render_failed` / `render_interrupted` |

pending 只允许该 type 的第一个 `*_preparing` code；running 才允许按表内顺序使用全部
非 terminal code。
phase 只在已有 adapter/application 边界推进，不解释为百分比或预计剩余时间。phase 只能
向前；重复写当前 phase 可幂等，不能回退。

### 5.2 exact result ref

`transcribe_source` 成功结果：

```json
{
  "kind": "transcript",
  "source_id": "source_a",
  "transcript_version_id": "tr_fixture",
  "schema_version": 1,
  "content_hash": "1111111111111111111111111111111111111111111111111111111111111111",
  "project_revision": 8
}
```

只有 immutable Transcript 已发布、Project active Transcript 已提交，且 scope 内 active
WorkflowRun 的 exact binding sync 已成功（或该 source 不属于 active run）后才能 succeeded。
`content_hash` 必须复用现有
`subject_content_hash("timed_transcript", schema_version, transcript.to_dict())`；raw ASR
不是 result ref。

`proxy_create` 成功结果：

```json
{
  "kind": "proxy",
  "source_id": "source_a",
  "cache_key": "9999999999999999999999999999999999999999999999999999999999999999",
  "manifest_schema_version": 1,
  "manifest_content_hash": "2222222222222222222222222222222222222222222222222222222222222222",
  "output_relative_path": "proxies/source_a/9999999999999999999999999999999999999999999999999999999999999999/proxy.mp4",
  "output_size": 1048576,
  "output_sha256_head_tail": "3333333333333333333333333333333333333333333333333333333333333333",
  "project_revision": 7
}
```

manifest hash 使用 canonical `ProxyManifest.to_dict()`；MP4 身份复用现有
`ProxyOutput(size, sha256_head_tail)`，不另造全文件 hash。只有 output/manifest 成对发布
并由现有 `read_proxy` 完整验证后才能 succeeded。

`approve_export` 成功结果：

```json
{
  "kind": "render",
  "run_id": "run_fixture",
  "render_plan_ref": {
    "artifact_id": "render_fixture",
    "schema_version": 2,
    "content_hash": "abababababababababababababababababababababababababababababababab"
  },
  "mp4_ref": {
    "kind": "mp4",
    "artifact_id": "render_fixture",
    "schema_version": 1,
    "content_hash": "4444444444444444444444444444444444444444444444444444444444444444",
    "project_relative_path": "renders/render_fixture.mp4"
  },
  "manifest_ref": {
    "kind": "manifest",
    "artifact_id": "render_fixture",
    "schema_version": 3,
    "content_hash": "5555555555555555555555555555555555555555555555555555555555555555",
    "project_relative_path": "renders/render_fixture.manifest.json"
  },
  "approve_export_receipt_ref": {
    "action_id": "action_export_fixture",
    "receipt_schema_version": 1,
    "receipt_hash": "6666666666666666666666666666666666666666666666666666666666666666"
  },
  "project_revision": 7
}
```

只有既有 workflow transaction 已发布 Render Plan、MP4、manifest、approval、receipt 和
Run after image后才能写 succeeded。单素材/多素材 manifest schema 继续只允许既有 2/3，
并必须分别配对 actual Render Plan schema 1/2；hash 继续使用 workflow
`_manifest_hash`。OperationRecord 不自行写 Run 或推进 stage。

单素材成功的配对证据固定为：

```json
{
  "decision_ref": {
    "artifact_id": "decision_single",
    "schema_version": 1,
    "content_hash": "7777777777777777777777777777777777777777777777777777777777777777"
  },
  "render_plan_ref": {
    "artifact_id": "render_single",
    "schema_version": 1,
    "content_hash": "8888888888888888888888888888888888888888888888888888888888888888"
  },
  "manifest_ref": {
    "kind": "manifest",
    "artifact_id": "render_single",
    "schema_version": 2,
    "content_hash": "9999999999999999999999999999999999999999999999999999999999999999",
    "project_relative_path": "renders/render_single.manifest.json"
  }
}
```

### 5.3 closed error

failed/interrupted 的 `error` 恰好为：

```json
{
  "code": "media_operation_failed",
  "responsibility": "asr_worker",
  "action": "run_asr_worker",
  "message_code": "transcription_failed"
}
```

`code` 只允许 `media_operation_failed`、`media_operation_interrupted`。responsibility
只允许：

- `roughcut_core`
- `asr_worker`
- `ffmpeg_proxy`
- `ffmpeg_render`
- `host`
- `user_input`

action 只允许：

- ASR：`validate_transcription_basis`、`decode_transcription_audio`、
  `run_asr_worker`、`normalize_transcript`、`publish_transcript`、
  `synchronize_transcript_binding`
- Proxy：`validate_proxy_basis`、`encode_proxy`、`verify_proxy`、`publish_proxy`
- Render：`validate_export_basis`、`encode_render`、`verify_render`、
  `publish_export_transaction`
- 共同中断：`interrupt_media_operation`、`recover_abandoned_media_operation`

`message_code` 必须等于该 type 的 failed/interrupted terminal phase。error 不保存自由文本、
绝对路径、token、命令行、原始 locator、下载 URL、stderr 或 traceback。面向用户的解释由
closed code + responsibility + action 派生。

责任归属固定：

| failure owner | responsibility |
| --- | --- |
| record/store、canonical parse、normalize/publish/binding coordinator | `roughcut_core` |
| FunASR isolated worker 启动、执行或 worker output | `asr_worker` |
| Proxy 的 FFmpeg encode/decode verification | `ffmpeg_proxy` |
| 正式 Render 的 FFmpeg encode/output verification | `ffmpeg_render` |
| Host 明确终止同步调用或传递 KeyboardInterrupt | `host` |
| stale revision/ref/approval、非法调用输入或 core 收到的显式用户取消 | `user_input` |

Host 临时 task ID 仅从 Host UI 消失不属于失败；只要 writer lock 仍被持有，record 必须保持
running。即使 core/transport worker 或其 lock 已消失，只要 core-owned media child 仍
可能修改 staging，record 也不得收敛为 terminal `interrupted`。

## 6. 幂等、status、stale 与取消

启动入口必须先按 operation 是否存在分成两条且顺序不可交换的路径：

1. **已存在 operation**：先验证 lexical Project scope、safe operation ID，并从本次稳定
   公开请求重建第 4.1 节 closed request projection/hash。相同 `request_hash` 立即回读原
   record；不得重建或比较历史 `input_hash`，也不得重新检查
   当前 stage、current Project revision、current WorkflowRun record hash、当前 approval
   readiness 或当前 runtime selection，也不得启动 worker。不同 `request_hash` 返回
   `operation_input_conflict`。因此 ASR 成功把 revision 7 提交为 8，以及 Render 成功改变
   run stage/current record 后，同一次 operation 的响应丢失重试仍回读原 succeeded。
2. **不存在 operation**：才执行当前 public hard gate、approval、expected revision、
   source/ref 和持久 runtime revalidation。全部通过后由 core 计算 stable
   `request_hash` 与完整 execution `input_hash`、取得 writer、原子发布 pending并启动
   媒体任务；失败为零 worker、零 record、零 artifact。

`request_hash` 必须只来自调用 envelope/closed 公开业务输入；不得读取当前 runtime、
Source snapshot、Project after image、WorkflowRun after record、Decision 或其他执行 basis。
Render 的 request/input projection 分别遵守第 4.1.3/4.4 节。

- 相同 operation ID、相同 closed request hash：只回读现有 record；running 不启动第二个
  worker，terminal 不重做媒体或重发 artifact。
- 相同 ID、不同 source ID、expected revision、speaker/timeout、workflow action input 等
  任一 closed request 字段：
  `operation_input_conflict`，在媒体、candidate、Project、WorkflowRun、approval、receipt
  写入前拒绝。
- 显式重跑使用新 operation ID；interrupted/failed 不能回到 running。
- Render 的新 operation ID 仍必须经过 `approve_export` façade。若前一
  failed/interrupted Render 留下 tracked staging，新启动调用只能使用有限工作流契约
  第 10.4 节的单一清理分支：tracked Render 的合法临时 workspace 必须具有由 closed
  owner、旧 operation/action identity 和 export basis 精确确定的 core-owned identity。
  在 export claim 下，以旧 action ID 与 workflow action input hash 重建并匹配 exact
  `approve_export` request hash，确认旧 record 属于同 Project/type 且 terminal 为
  failed/interrupted，并确认其媒体子进程已经停止且不能继续写入；再按既有锁序确认
  current run/export basis 完全相同、旧 action 没有 TransactionMarker/ActionReceipt，
  且 closed owner/tree 中恰好存在该一个精确临时 workspace。全部通过才删除明确归属旧
  operation 的节点，随后由新 action 正常 preflight并只启动一次 Render。live writer、
  仍可能写入的 media child、succeeded/nonterminal/missing/mismatched record、basis
  变化、marker/receipt、symlink、hardlink、额外节点、多个临时 workspace 或
  identity/owner/tree 不符，一律保留现场并 `workflow_recovery_conflict`。不得使用宽泛
  glob、扫描 artifact 猜测成功或清理未知目录。
- status 发现 pending/running 且 writer lock 仍被持有时原样返回；worker/lock 消失时，
  只有先保证本 operation 的全部 core-owned media child 已停止且不能继续写入，才可在
  同一 lock 内重读并写 interrupted。status 不得仅凭 Host task、stdio connection、PID
  或 writer lock 消失发布 terminal；无法证明 child 已收敛时保持非终态并 fail closed。
- status 不扫描 raw ASR、Transcript、proxy cache、render staging、receipt 或目录来猜
  succeeded，也不清理 candidate/staging。已提交 artifact 与 interrupted record 可以同时
  存在；artifact 仍由其领域 store 验证，record 如实表示 writer 未完成 terminal publish。
- 成功 record 已落盘但响应丢失时，同 ID/request 只回读 succeeded result。
- KeyboardInterrupt、Host 明确取消或 adapter 的既有取消异常沿现有媒体清理语义传播，
  coordinator 先保证所属媒体子进程已停止且不能继续写入，最后才记录 interrupted；
  OperationRecord 不增加新的 cancel API。

不存在 operation 的 preflight 才取得现有 public hard gate/Project lock/claim，恢复既有
workflow transaction，验证 active run/授权/approval/expected revision/exact refs和持久
runtime，再计算 closed input。preflight stale 在 operation 创建前使用现有稳定错误码
拒绝，零 worker/record/artifact；媒体运行后 revalidation stale 则保留既有 before 状态、
按 adapter 现有规则清理本次 candidate，并把 record 写 failed，
responsibility=`user_input`。已存在 operation 的 readback 不进入这条 preflight。

纯 operation status 是另一条只读路径：只验证 lexical Project scope、safe operation ID 和
record closed schema后读取/收敛状态；它不接收 request payload，也不要求当前 runtime、
Project revision、Source、WorkflowRun stage 或 approval仍等于任务启动时状态。

关系固定如下：

- ASR 继续要求 active scope、exact source 的 transcribe/speaker 授权和 current scope
  approval；成功 Project revision 从 expected 增加 1，binding sync 不增加 revision、
  不推进 stage或签发 approval。
- Proxy 继续属于兼容白名单：不要求 active run，不推进 stage，不修改 Project revision。
  Proxy 默认不阻塞 workflow。
- Render 只能由 `approve_export` façade 发起；record 不授予批准。export claim 继续唯一
  控制正式 Render 并发；正式 workflow transaction 继续唯一发布结果并完成既有 action
  transition。

第二阶段 B 首版不得把媒体 OperationRecord ID 写入或移出 WorkflowRun
`readiness_basis.blocking_operation_ids`：ASR 继续由既有
`required_transcripts_ready` 阻止后续动作；Render 运行中继续由 export claim和既有
allowed-action 派生表达；Proxy 默认不阻塞业务门。Host 必须保留任务启动前获得的
operation ID，并通过后续纯 operation status 直接回读。这样 record 与 Run 不发生双写，
OperationRecord 的任何 transition 都不修改 WorkflowRun。若未来真实 Review playback
证据要求 blocker，必须另行冻结，不得在本阶段预建或重新打开已完成的有限工作流门。

## 7. 现有提交点与崩溃语义

| operation | preflight/current objects | candidate/staging | 领域提交边界 | record succeeded 时点 | hard-exit 结果 |
| --- | --- | --- | --- | --- | --- |
| ASR | Project revision、Source snapshot/fingerprint、scope authorization/approval、runtime selection | `raw-asr/<source>/<run>.json`、内存 Transcript、原子 Transcript temp | immutable Transcript → Project active version/revision → exact Workflow binding sync | binding sync 完成后 | Transcript 前：raw 可留诊断；Project 后：Project/Transcript 保持，status 只记 interrupted，后续 workflow status 可独立修复 binding |
| Proxy | Project revision、Source snapshot/fingerprint/probe、derived profile/cache key、tool refs | `proxies/<source>/.candidate-*` 下 MP4/manifest | 现有 `_publish_proxy` 成对发布到 cache-key 目录；Project 不写 | `read_proxy` 验证 ready 后 | candidate/单文件 partial 保留证据或由既有异常清理；status 不采用、不删除、不猜 ready |
| Render | export claim、WorkflowRun/export_review、roughcut approval receipt、Decision、export basis、Project revision、Prepared Render Plan | 现有 closed owner + `workflow/export-staging/<staging-id>` | 现有 TransactionMarker → plan/MP4/manifest → Run/approval/receipt | workflow receipt 与 Run after 全部发布后 | owner/staging 继续由有限 workflow 显式重入/恢复；operation status 不清理、不推进 stage |

恢复再次中断时保留 record/temp 或现有 domain marker/owner 证据；下一次只重复对应的有限
reconciler。不得跨系统扫描目录、按时间选 latest 或把存在文件等同成功。

## 7.1 原生 stdio MCP 长调用存活性

一个业务 `tools/call` 长时间执行时，同一 stdio server 必须继续及时读取并响应标准
MCP/JSON-RPC `ping`；`ping` 只证明 transport liveness，不读取或修改 Project、WorkflowRun、
OperationRecord 或 staging。同一 stdio server 同时最多执行一个业务 `tools/call`，transport
不得在长调用旁并发分派第二个 application mutation。

该存活性不得通过通用任务队列、scheduler、daemon、heartbeat、PID lease、自动重试或
任意并发 workflow mutation 实现。Host 重连或重发相同 operation/action ID 时，既有第 6
节 existing-first 顺序不变：相同 request 只回读既有 record/receipt，零第二媒体 worker；
不同 request 仍冲突。stdio control plane 的可响应性不改变 operation 状态、writer 所有权
或业务提交边界。

标准 MCP `notifications/cancelled` 只标记 Host 已放弃匹配请求的晚到响应；它不表示
Roughcut 已取消媒体业务，不终止 worker/媒体 child，也不写 `cancelled` operation 状态。
业务继续执行并释放单业务槽，最终结果仍由原 operation ID 回读。未来若真实用户需要中途
停止成本，必须另行冻结授权、子进程终止、candidate 清理与 terminal 状态；不得从 transport
notification 推导通用 cancel framework。

## 8. traceability 与第二阶段 B

| operation | existing public gate | input owner | phase seam | result owner | existing concurrency/cleanup | Workflow effect |
| --- | --- | --- | --- | --- | --- | --- |
| `transcribe_source` | existing operation 先按 Project scope/source/revision/speaker/timeout request hash 回读；新 operation 才执行 `protected_write` scope/source/ASR/speaker approval + expected Project revision | request：stable public fields；input：`SourceAsset`、persistent runtime binding、Python canonical receipt hash、component receipt values、FFmpeg tool selection hash、`FunASRConfig` | transcription application + PCM decoder + FunASR runner + normalize/publish/binding sync | `TimedTranscript` + Project active version | Project write lock；现有 raw/temp/Transcript rollback | 只同步 exact binding；record 不写 readiness/stage |
| `proxy_create` | existing operation 先按 Project scope/source/revision request hash 回读；新 operation 才验证 Project revision、Source snapshot/fingerprint；无 active-run 要求 | request：stable public fields；input：`derive_proxy_profile` + `proxy_cache_key` + persistent runtime/tool selection hashes | proxy application + FFmpeg encode/verify/publish | `ProxyManifest` + `ProxyOutput` | deterministic cache target、candidate temp、成对 publish cleanup | 无；record 不写 readiness |
| `approve_export` | existing operation 先按 Project scope/run/action/action-input request hash 回读；新 operation 才验证 workflow façade export basis/approval/ref + export claim | request：stable run/action identity；input：ReceiptRef、Decision、export dependency/basis、actual Prepared Render Plan | existing staging owner + render encode/verify + workflow publish | existing Render Plan/OutputRefs/ActionReceipt | export claim、owner/staging、TransactionMarker recovery | 仅既有 approve_export transaction；record 不写 readiness/stage |

第二阶段 B 预计只修改：

- 新增 `core/src/roughcut/domain/media_operation.py`
- 新增 `core/src/roughcut/adapters/media_operation_store.py`
- 新增 `core/src/roughcut/application/media_operations.py`
- 最小接缝：
  `core/src/roughcut/application/transcription.py`、
  `core/src/roughcut/adapters/funasr/runner.py`、
  `core/src/roughcut/application/proxies.py`、
  `core/src/roughcut/adapters/ffmpeg/proxy.py`、
  `core/src/roughcut/application/workflows.py`、
  `core/src/roughcut/application/renders.py`
- 对应 domain/store/application/fixture/subprocess tests。

第二阶段 B 已完成并通过主评审，且未修改 CLI/MCP、tool schema、Skills、Review UI 或
artifact schema；内部 coordinator 接受 exact operation ID。第三阶段已经把纯
`media_operation_status(project_path, operation_id)` 接到版本化 CLI/MCP 与 canonical
Skill，但这只能证明已持有 ID 的 record 可回读，不能证明真实公开媒体启动会创建 record。
因此在整个运行可靠性门关闭前，必须另行完成下述“公开媒体启动闭环”前置项；该前置项不
重新打开第二阶段 A/B，也不改变第三阶段纯 status 已通过主评审的状态。

### 8.1 公开媒体启动闭环：tool schema 25

本节契约、生产接线及有界 P1 已完成并通过主评审；tool schema 已一次升级到 25。
三种入口继续手写，不增加 generic start、任意 payload、队列、worker、
daemon、scheduler、heartbeat、PID lease、自动重试或恢复。

#### 8.1.1 operation ID 的生成与传递

- ASR/Proxy 的 Host invocation layer 在创建同步 CLI 进程或发出 MCP call **之前**生成并
  保存稳定 operation ID。canonical Host 格式固定为 `op_` 加 32 个小写十六进制 UUID v4
  字符；core 继续接受并验证既有 safe operation ID 语法，以支持 CLI 调用方和幂等回读。
  Agent 只原样传递或回读该 ID，不计算 `request_hash`/`input_hash`。不新增 operation-ID
  allocator tool，也不允许 core 在 worker 已启动后才返回 ID。
- CLI 复用既有全局 `--operation-id` 字段：
  `transcribe-source --operation-id <id>` 与
  `proxy-create --operation-id <id>`。MCP 的既有同名工具分别增加 required
  `operation_id` 字段；除此之外不新建媒体 start tool。
- Render 不改变九个 workflow action/input。`workflow_action.action_id` 同时就是
  `approve_export` operation ID；公开 dispatcher 在 action 为 `approve_export` 时只路由
  `run_approve_export_operation(project_path, run_id, action_id, action_input)`，其他八个
  action 仍走既有 façade。不得先调用未追踪 `_workflow_export` 再补 record。

#### 8.1.2 canonical ASR runtime

tool schema 25 的 tracked `transcribe_source` 公开请求恰好包含
`project_path/operation_id/source_id/expected_revision/speaker_diarization`，其中
`speaker_diarization` 可省略且默认为 false；固定 timeout 由 core 决定，不是 Host 请求
字段。`funasr_python`、`model_root` 和 `speaker_model_path` 从该 CLI/MCP 请求 schema
删除，若通过未知字段、旧 CLI option 或直接 handler 参数提交则以
`invalid_arguments` 在创建 record/启动 worker 前拒绝。

tracked coordinator 只读取并验证版本化持久 `runtime.json`，由 core 计算完整 input
identity。临时参数和 `ROUGHCUT_*` 进程变量只能用于明确的 diagnostics/自动测试，不得为
公开 tracked ASR 选择 Python、模型或 speaker 路径。低层 `transcribe_source` service
保留为 coordinator 内部 participant，但 CLI/MCP dispatcher 不再有直达它的并行公开路径，
也不新增 legacy/bypass tool。

`proxy_create` 的 tool schema 25 请求恰好包含
`project_path/operation_id/source_id/expected_revision`；它同样只使用持久 runtime/tool
binding。Proxy 仍不要求 active WorkflowRun。

#### 8.1.3 closed success/readback/error envelope

CLI 与 MCP 继续共享 common fields：
`schema_version/tool_schema_version/core_version/platform/ok`。schema 25 的三种公开启动
成功响应不允许其他顶层字段：

| entry | exact additional fields |
| --- | --- |
| `transcribe_source` | `media_operation`、`operation_readback`、`transcript` |
| `proxy_create` | `media_operation`、`operation_readback`、`proxy` |
| `workflow_action(approve_export)` | `media_operation`、`operation_readback`、`workflow_run`、`receipt`、`status` |

`media_operation` 是 exact closed schema-1 record；`operation_readback` 是 boolean。首次
同步执行成功时它为 false，ASR/Proxy 的 legacy result object 与 approve_export 的既有
workflow result 均正常返回。existing operation 同 request 时它为 true，不启动 worker、
不重发 artifact；ASR/Proxy 的 `transcript`/`proxy` 固定为 null，调用方以 record 的
terminal result ref 为真相。approve_export 的 `workflow_run/status` 使用既有 pure read，
`receipt` 只在 exact receipt 已发布时返回，否则为 null；不得扫描 Render、staging、
manifest 或目录补结果。existing record 即使为 pending/running/failed/interrupted，也
仍是成功的幂等 readback envelope，业务结果字段按上述规则为 null。

所有失败响应的顶层字段恰好为 common fields 加 `error`，其中 `ok=false` 且
`error` 恰好为 `{ "code": "<stable-code>" }`；不附带路径、stderr、token、命令行、
runtime override 或自由文本。缺失/错误类型/未知请求字段返回 `invalid_arguments`，unsafe
operation ID 或 closed record 损坏返回既有 `operation_integrity_error`，同 ID/different
request 返回 `operation_input_conflict`；其余既有 public hard-gate/workflow/media
错误码保持责任主体和语义。Host 已在调用前持有 ID，因此响应丢失或进程硬退出后只用该 ID
调用纯 status；如果 preflight 在 pending record 原子写入前失败，status 如实返回
`operation_not_found`，不能猜测任务曾开始。

#### 8.1.4 版本与兼容边界

tool schema 24 精确描述上一版 status-only 生产面；本次公开启动接线已一次升级到
25：ASR/Proxy 新增 required `operation_id`，tracked ASR 删除三项临时 runtime override，
approve_export input 与九个 action 均不变但输出增加上述 OperationRecord 字段；纯 status
语义不变，只随 common field 报告 25。schema-24 Host 必须在 Host Package/tool discovery
刷新后才调用 schema 25，不做静默降级或双路 dispatcher。Project、WorkflowRun、
Transcript、Proxy、Render Plan/manifest、action/input 和 `blocking_operation_ids` 均不
修改。

下一轮验收必须使用 fixture/fake worker/小型临时字节文件证明：

1. CLI/MCP 的 ASR、Proxy、`workflow_action(approve_export)` 真实公开入口分别命中已有三个
   coordinator，Host 在调用前已经持有对应 ID；
2. 模拟响应丢失及 pending/running 节点硬退出后，新的公开 status 调用读取同一 record并按
   writer 状态收敛；
3. same ID/same request 为 readback 且零额外 worker，same ID/different request 为
   `operation_input_conflict`；
4. 不运行真实用户媒体、不下载 FunASR、不调用真实 FFmpeg、不执行正式 Render。

WorkBuddy 复验必须写入新的独立报告文件，并把“人工预置合成 running record 的纯 status
冒烟”与“真实公开 start→coordinator→同 ID status 的自动集成证据”分节记录；前者不能
替代后者。

第二阶段 B 验收：

1. 三种 operation 的 closed parse/roundtrip、request/input canonical hash、单向
   transition、同 ID
   幂等/冲突和 Project scope/path integrity 全通过；
2. 三条 fixture 媒体链实际写 phase/result，live writer 与真实子进程 hard exit 行为一致；
3. existing operation 同 request 在 revision/runtime/Source/stage/Run after 变化后仍
   回读，different request
   conflict；只有 missing operation 执行 stale/public/runtime preflight且零媒体调用；
   运行后 stale 使用既有 candidate cleanup且 record failed；
4. ASR Project/Transcript/binding、Proxy pair publish、Render owner/claim/transaction 的既有
   故障注入回归保持通过；
5. OperationRecord 不改变 Project/WorkflowRun/ContentDraft/artifact schema，不写
   `blocking_operation_ids`，不推进 stage；
6. 不运行真实媒体或下载，Windows 只报告 fixture/static branch，不冒充实机。

预计 4–7 个集中工程日。若实现需要通用 operation 基类/注册表、修改 frozen workflow
action/input、改变 Project/WorkflowRun/Transcript/Proxy/Render schema、自动重启/扫描推断
成功、PID/lease/heartbeat、数据库/daemon/队列/scheduler，或无法在现有 candidate/owner/
TransactionMarker 边界内接线，立即停止复评，不自行扩张。

## 9. 稳定错误码与未实施范围

媒体 record/store 只增加：

- `operation_not_found`
- `operation_input_conflict`
- `operation_transition_not_allowed`
- `operation_integrity_error`
- `operation_write_failed`
- `media_operation_failed`
- `media_operation_interrupted`

现有 public hard gate/stale/workflow/媒体错误码保持不变；OperationRecord 不把它们重映射成
授权凭据。第二阶段 A/B 已完成并通过主评审；第三阶段已将 Project-media operation
status 接到 CLI/MCP/tool schema/canonical Skills，并通过 WorkBuddy 5.3.5 原生合成
record 短冒烟且已通过主评审。8.1 的真实公开启动闭环生产接线及有界 P1 已完成并通过
主评审，长任务可见性门已经关闭。后续 W5/M2.6 已通过；M3.1 docs-only 范围复核确认
本契约已经覆盖当前真实需求，不修改 schema、operation type、store 或公开接口。Review、
Host 自动轮询、自动重试、后台恢复、worker 续跑、队列和调度仍不在当前实现范围；未来只
能由新的、重复且可复现的用户证据另立契约。
