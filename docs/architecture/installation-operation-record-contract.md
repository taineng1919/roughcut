# Installation OperationRecord Contract


配套 vectors：`core/tests/fixtures/installation-operation-record-vectors.json`

## 1. 边界

本契约只回答一次已批准组件安装当前处于什么可信状态。它不排队、不保存完整日志、不
自动重试、不恢复 worker、不提供 cancel、不保存 PID、不伪造百分比，也不接入 ASR、
Proxy 或 Render。

OperationRecord 不是 WorkflowRun、ApprovalRecord、Draft Workspace Checkpoint 或
runtime binding。它不得推进有限工作流 stage。

## 2. 受控路径与锁

安装记录的唯一规范路径是：

```text
<install-root>/operations/component-installation/<operation-id>.json
```

原子写临时文件固定为：

```text
<install-root>/operations/component-installation/.<operation-id>.json.tmp
```

瞬时 writer lock 固定为：

```text
<install-root>/operations/component-installation/.<operation-id>.writer.lock
```

调用方只能提供 safe operation ID，不能提供 record、temp 或 lock 路径。install root、
`operations`、`component-installation`、record、temp 和 lock 的现存节点都必须用
`lstat` 重新验证；目录不得为 symlink，文件必须是普通文件且只有一个 hard link。

普通状态读取在目录或 record 不存在时不得创建目录。新 apply 可以创建受控目录。record
使用同目录临时文件、file fsync、严格 readback、atomic replace 和 parent directory
fsync。写入失败时旧 record 保持权威，不得返回伪成功。

writer lock 只表示当前是否仍有实际安装 writer。它不是 PID、lease、心跳或持久状态。
POSIX 使用 non-blocking `flock`，Windows 使用 non-blocking `msvcrt.locking`。进程退出后
内核释放锁；lock 文件本身可以保留。

## 3. closed schema 1

record 顶层字段必须恰好如下：

```json
{
  "schema_version": 1,
  "operation_id": "op_0123456789abcdef0123456789abcdef",
  "scope": {
    "kind": "installation",
    "install_root_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "operation_type": "component_installation",
  "input_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "status": "running",
  "phase_message_code": "component_installation_downloading",
  "created_at": "2026-07-29T08:00:00.000000Z",
  "started_at": "2026-07-29T08:00:01.000000Z",
  "updated_at": "2026-07-29T08:00:02.000000Z",
  "finished_at": null,
  "result_ref": null,
  "error": null
}
```

规则：

- `operation_id` 必须匹配 `op_[A-Za-z0-9_-]{1,125}`。
- `scope` 恰好含 `kind/install_root_hash`；kind 固定为 `installation`。hash 是 core
  根据 canonical install root 计算的不透明身份，响应不返回路径。
- `operation_type` 固定为 `component_installation`。
- `input_hash` 是 core 对 `component_installation`、approved full plan hash 和 schema
  版本做 canonical JSON v1 后的 SHA-256；不保存 external 路径、URL 或凭据。
- JSON 使用 UTF-8、Unicode NFC、CRLF/CR→LF、key 排序、整数限定和 SHA-256；
  canonical 规则复用 `canonical_json_v1`。拒绝 duplicate key、float、BOM、未知字段和
  未知 schema。
- `operation_id/scope/operation_type/input_hash/created_at` 创建后不可改变。

### 3.1 result ref

成功时 `result_ref` 恰好为：

```json
{
  "kind": "runtime_binding",
  "schema_version": 1,
  "approved_plan_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "runtime_binding_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "component_manifest_sha256": null
}
```

它只保存已发布 runtime binding 的精确文件 SHA-256、批准 plan hash 和可选 manifest
SHA-256，不保存路径或 binding 正文。这里的 `result_ref.schema_version=1` 只版本化
OperationRecord 内的 result-ref 形状，不是 runtime binding payload schema；被引用的当前
runtime binding payload 为 schema 2。

### 3.2 closed error

`failed/interrupted` 的 error 使用 closed union。普通安装失败恰好含：

```json
{
  "code": "component_installation_failed",
  "responsibility": "roughcut_component_installer",
  "action": "install_components",
  "message_code": "component_installation_failed"
}
```

runtime binding publication failure 可以在上述四个字段之外增加一个可选的
`reason_code`。它不是自由文本，也不改变 OperationRecord schema 1；旧的四字段 record
仍然可以读取和逐字节回写。允许的值只有：

- `runtime_publish_stale_plan`
- `runtime_publish_existing_binding_invalid`
- `runtime_publish_lock_failed`
- `runtime_publish_atomic_replace_failed`
- `runtime_publish_binding_validation_failed`
- `runtime_publish_failed`

例如 stale-plan CAS 的 bounded error 是：

```json
{
  "code": "component_installation_failed",
  "responsibility": "roughcut_runtime_binding",
  "action": "publish_runtime_binding",
  "message_code": "component_installation_failed",
  "reason_code": "runtime_publish_stale_plan"
}
```

responsibility 固定为：

- `roughcut_bootstrap`
- `roughcut_component_installer`
- `roughcut_runtime_binding`
- `python_https_runtime`
- `component_artifact`
- `user_input`

action 固定为：

- `install_components`
- `publish_runtime_binding`
- `download_component_artifact`
- `verify_component_artifact`
- `validate_approved_full_plan`
- `interrupt_component_installation`
- `recover_abandoned_component_installation`

error 不保存底层绝对路径、下载 URL、token、traceback 或任意自由文本。

### 3.3 P1/P2 typed failure boundary

OperationRecord schema 1 不按 error string 中的 `certificate`、`download`、`checksum`、
`full verification` 等子串猜责任主体。installer 在 typed boundary 处先产生受控失败
类型，再映射到既有的 `responsibility/action` union；文本内容永远不能改变 mapping：

| typed boundary | responsibility | action |
| --- | --- | --- |
| stale/approved-plan input mismatch | `user_input` | `validate_approved_full_plan` |
| network/TLS/HTTP/socket/body-read transport | `python_https_runtime` | `download_component_artifact` |
| artifact size/SHA/payload/receipt integrity | `component_artifact` | `verify_component_artifact` |
| runtime publication | `roughcut_runtime_binding` | `publish_runtime_binding` |
| staged runtime/probe/framing failure | `roughcut_component_installer` | `install_components` |

只有真正的 stale/approved-plan 输入错误才能映射为 `user_input` /
`validate_approved_full_plan`。staged runtime、isolated probe、protocol framing、未知
字段或 closed payload failure 即使底层文本恰好含有 `full verification`，也必须映射为
`roughcut_component_installer` / `install_components`。network/TLS/HTTP/socket/body-read
transport 必须保留 `python_https_runtime` / `download_component_artifact`；artifact
size/SHA/payload/receipt integrity 必须保留 `component_artifact` /
`verify_component_artifact`。禁止恢复按字符串子串分类的契约。

terminal phase 继续使用 `component_installation_failed`。OperationRecord 不增加 traceback、
自由文本、绝对路径、native dump 或 schema 2 字段；failure 只能写本节既有 closed error
对象和上述 bounded `reason_code`。runtime publication 的底层异常、路径和 traceback 不会
持久化。对 record 尚不存在的新 operation，stale approved-plan preflight 失败仍向调用方返回
`responsibility=user_input` / `action=validate_approved_full_plan`，但不得为了保存该错误而创建
OperationRecord。

## 4. 状态与 phase

唯一合法状态转换：

```text
pending -> running -> succeeded
                   -> failed
                   -> interrupted
```

terminal 状态不得回到 running。phase/status 组合固定：

| status | phase_message_code |
| --- | --- |
| pending | `component_installation_preparing` |
| running | `component_installation_preparing`、`component_installation_downloading`、`component_installation_installing`、`component_installation_verifying`、`component_installation_publishing_runtime` |
| succeeded | `component_installation_succeeded` |
| failed | `component_installation_failed` |
| interrupted | `component_installation_interrupted` |

pending 的 started/finished 为 null；running 的 started 非 null、finished 为 null；terminal
的 started/finished 非 null。只有 succeeded 有 result_ref；只有 failed/interrupted 有
error。updated_at 只能向前。

## 5. 幂等、硬退出与纯状态读取

- 相同 operation ID、相同 input hash：回读同一 record；terminal 不重新 apply。
- 相同 ID、不同 input hash：`operation_input_conflict`，零 plan/apply/download/publish。
- apply 持有 writer lock 时，纯状态读取返回现有 running record。
- record 为 running 且 writer lock 已释放：状态读取在取得同一 writer lock 后原子收敛为
  interrupted。它不猜成功、不重跑 apply。
- status 与 apply 竞争时必须在锁内重读 record，不能用 PID 或 Host task ID 作真相。
- status 入口只接受 install root 与 operation ID；不得生成 plan、访问网络、安装、验证、
  修复或发布 runtime binding。

稳定错误码只包括：

- `operation_not_found`
- `operation_input_conflict`
- `operation_transition_not_allowed`
- `operation_integrity_error`
- `operation_write_failed`
- `component_installation_failed`
- `component_installation_interrupted`

错误必须以 `Roughcut installation operation ...` 或具体安装责任主体开头。

## 6. component plan/apply

quick plan (`verification_mode=quick`) 仅用于诊断，不能发布 runtime binding。本 P1/P2
契约实现后所有 component plan 均为 schema 2；full plan 始终返回 `plan_hash`。当前
生产 component plan 在实现前仍为 schema 1。只有
`apply_supported_on_this_host=true` 且不存在 blocking user action 时，bootstrap 才返回
非 null 的 installation-operation approval envelope（其中含新的 operation ID 与批准的
plan hash）。blocking 或 cross-target-only plan 的 `installation_operation` 固定为 null，
不能 apply。runtime binding 当前为 schema 2；installation OperationRecord 仍是本文件冻结的
schema 1。

用户解决 VC++ 或其他 blocking prerequisite 后必须重新 plan，取得新的 `plan_hash` 和
新的 operation ID。schema-1 plan hash 必然与 schema-2 重算 hash 不同，并在 stale 校验中
拒绝；不建立新的 operation-ID provenance framework。apply 的统一顺序固定为：

1. 先按 existing-first 查找同 operation ID。record 已存在且 input hash 相同时原样回读，
   不重建 plan、不检查当前 prerequisite；input hash 不同时返回
   `operation_input_conflict`，同样不重建 plan。
2. record 不存在时，取得该 operation 的 writer ownership，并在锁内重读 record。若竞争者
   已创建 record，仍按第 1 步的同 input 回读/不同 input conflict 收敛。
3. 锁内重读后 record 仍不存在时，才只读重建 schema-2 full plan，并同时验证 exact
   approved plan hash、`apply_supported_on_this_host=true`、零 blocking user action，以及
   install/managed/cache 的全部 path budget 均通过。
4. 任一 preflight 失败都释放 writer ownership；零 OperationRecord、零 cache/staging/runtime
   写入、零下载。stale approved plan 使用既有 `user_input` /
   `validate_approved_full_plan` typed mapping，但不创建失败 record。
5. 全部 preflight 通过后才创建 pending record，再进入既有 running phase。

pending record 之后只在既有固定边界写 phase：

1. preparing：接收并采用已经通过的 exact preflight；不得在此首次检查 prerequisite、
   stale plan 或 path budget；
2. downloading：下载缺失 artifact；
3. installing：发布缺失 managed components；
4. verifying：完整校验 post-install plan；
5. publishing runtime：原子发布 runtime binding；
6. succeeded/failed/interrupted。

成功响应丢失后，同 ID/同 input 回读 succeeded record，不重复安装或发布。明确异常记录
failed；KeyboardInterrupt 或已捕获进程中断记录 interrupted，并继续既有异常清理语义。
真正硬退出只由下一次纯 status 按 writer lock 收敛 interrupted。

## 7. 本阶段未实现

本阶段不接 ASR、Proxy、Render、公开 CLI/MCP operation 工具、canonical Skills、Host
轮询/回读、自动恢复、自动重试、daemon、数据库、队列、scheduler、lease、心跳、完整
日志或通用 OperationRecord framework。
