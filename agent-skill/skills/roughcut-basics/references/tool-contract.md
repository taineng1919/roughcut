# Agent tool contract

Schema version: 1
Tool schema version: 32

`roughcut` and the local `roughcut-mcp` server expose the same application
services. Their successful payloads include `schema_version`,
`tool_schema_version`, `core_version`, `source_commit`, and `ok`.
`source_commit` is the exact 40-character committed HEAD SHA injected by the
release builder in staging (shared by wheel and source bundle); dev checkouts
report `null`. It is an additive build-identity field and does not bump
tool schema (still 32); bootstrap requires full identity equality
(`schema_version` + `core_version` + `tool_schema_version` + `source_commit`)
for REUSE, any difference → UPDATE.

Tool schema 32 is the current production contract. It retains every schema-31
entry byte-for-byte and adds exactly three closed Qwen credential tool entries:
`qwen_credential_configure`, `qwen_credential_readiness`, and
`qwen_credential_clear`. These are the minimal configuration/readiness public
surface authorized for the Roughcut user-level private credential store; see
"Qwen credential readiness" below. Schema 32 adds no other tool, action,
or error union; removes or renames no tool entry; and changes no workflow
stage, workflow gate, or `ApprovalRecord`. (The common version/health envelope
field `source_commit`, described above, rides in all successful payloads and
bootstrap responses without bumping schema/tool-schema versions; it is not a
tool-schema entry.) It does not add a second
transcription start path, and a configured credential does not enable Cloud
transcription.

Schema 31 historically retained the schema-30 public surface except for one
explicitly documented backward-compatible validation widening:
`workflow_action(submit_outline).input.sections` changed from
`minItems: 4, maxItems: 7` to `minItems: 1` with no `maxItems`. The array
remains required, ordered, and non-empty. Schema 31 added no tool, action,
field, or error union. The Outline artifact remains schema 1 and the workflow
action set remains unchanged. The `agent_propose`, `user_reference`, and
`user_directed` behavior is canonical Skill policy, not an additional
machine-protocol change.

Schema 30 historically retained every schema-29 entry byte-for-byte and added
the closed `approve_nle_export` entry for the independent Core-owned editable
NLE handoff approval/write boundary. It did not alter the existing
`approve_export` MP4 action. Earlier schema additions remain additive closed
input fields: each `align_multicam` auxiliary camera group may carry a non-empty
`source_pairs` array (see "Multicam exact source pairs" below), `approve_scope`
may carry the durable workflow-scoped Multicam Setup described below, and
`decision_read` provides one schema-dispatching decision read surface.
Integrity/read failures from that surface use `decision_read_integrity`; no
workflow action is added.

## Multicam exact source pairs (introduced in tool schema 30; retained by 32)

Each `align_multicam` auxiliary camera object keeps its required
`camera_id/ordered_source_ids` shape and may additionally carry exactly one
optional `source_pairs` array. Every item is exactly
`{ "main_source_id", "auxiliary_source_id" }`; both IDs must be safe IDs that
belong to this request's main camera group and that auxiliary camera group,
duplicate pairs are rejected, and core orders the surviving pairs
deterministically (ascending `main_source_id`, then ascending
`auxiliary_source_id`) before they enter the request hash and execution.
When `source_pairs` is present, core executes only the listed
pairs and never searches outside the declared synchronization group. Without
`source_pairs`, only a group with exactly one main Source and exactly one
auxiliary Source is accepted as the single unambiguous pair; any other
multi-file group fails closed before any media access instead of guessing an
index, count, or filename correspondence. The field is optional, additive, and
unknown fields inside a pair or camera object are still rejected.

`diagnostics` reports `runtime_binding` plus separate FunASR/model and
FFmpeg/ffprobe selections, and a Core-derived `alignment` readiness object:
`required_provider`, `required_provider_version`, `configured_provider`,
`configured_provider_version`, `interpreter`, `status`, and `production_ready`.
The required production provider is `audalign` version `1.3.1`;
`status` is closed to `available`, `missing`, `provider_mismatch`, or `invalid`.
Only `available` sets `production_ready: true`. A legal schema-2 historical
BBC `0.5.5` binding is `provider_mismatch` and is never treated as Audalign.
The object is derived from the normalized persistent RuntimeBinding and the
existing Audalign selection validator; it does not parse raw provider JSON, guess a
provider, or expose exception text. Each result labels its origin as exactly one of
`explicit_override`, `persistent_external`, `persistent_managed`, or
`unconfigured`. When no persistent binding exists it returns
`Roughcut runtime binding 未配置` and `next_action: component_plan`; it does not
probe an assumed default FunASR directory.

## M2.7 platform release split and current Core `0.2.8` provider

The fixed candidate `a81d36aff0d813419b40bcf3cc1ccd94a9dc0a73`, with core
version `0.1.13`, passed the one-time macOS source-checkout platform-slice
release gate. Its evidence remains historical; the subsequent bounded M3.4
Review UX revisions supersede it as the production candidate. No public binary,
PyPI package, or GitHub Release was published. The first `0.1.14` M3.4
implementation batch completed, but project-owner page acceptance failed; the
subsequent `0.1.15` candidate also failed its bounded acceptance/release
evidence. The unpublished `0.1.16` experiment and `0.2.0` are historical identities;
the current macOS trial Core is `0.2.8`. Its production writer is pinned Audalign
1.3.1 `CorrelationRecognizer`; old Fingerprint, waveform, and BBC artifacts remain
exact-ID readback only. This is not cross-platform M2.7
completion. Windows M2.7 is deferred. On
Windows, only the following three schema-26 direct CLI/MCP entries
are release-blocked and must fail closed before creating an operation, starting a
worker/child, touching multicam staging, or reading user media:

- `align_multicam` / `align-multicam`:
  `alignment_runtime_unavailable`;
- `multicam_parallel_render_prepare` / `multicam-parallel-render-prepare`:
  `parallel_render_runtime_unavailable`;
- `multicam_parallel_render_start` / `multicam-parallel-render-start`:
  `parallel_render_runtime_unavailable`.

CLI and MCP must share the single core-owned M2.7-specific
`require_m2_7_public_capability(entry_name)` guard. Each transport first completes
the existing closed request shape/type validation, then calls the guard, and only
after it passes may call the M2.7 application service or access Project, media,
alignment/parallel staging, OperationRecord, or worker/child state. The guard has
no environment-variable bypass or test-only production switch. The pure prepare
closed union is extended only by the existing
`parallel_render_runtime_unavailable`; no public error code is added.

The direct-entry guard does not cover the public `workflow_action(adopt_roughcut)` path.
`adopt_roughcut` itself must remain successful. Adoption preparation may construct and
persist the continuation requirement, deterministic `operation_id`, `alignment_id`,
`request_hash`, and writer-profile identity as immutable durable adoption metadata.
Those identities are not an Alignment `MediaOperationRecord`, operational execution,
or Windows M2.7 release. The existing preparation order is
`prepare_decision → build_multicam_alignment_continuation → construct deterministic
continuation operation/alignment identity → calculate request_hash → publish adoption /
receipt → run_multicam_alignment_continuation`; the release decision is not moved ahead
of durable identity/request construction.

The single required production correction has two guard sites. First,
`run_multicam_alignment_continuation` may read the `WorkflowRun`, durable continuation,
and requirement; it must then invoke the same core-owned M2.7 release decision before
any alignment operational work. On Windows denial, the adoption receipt remains
successful; the continuation must not call `run_align_multicam`, create an Alignment
`MediaOperationRecord`, read alignment runtime, user media, or artifact execution state,
create alignment workspace/staging, or start a worker/child. Second,
`multicam_alignment_status` must invoke the same decision for a required continuation
on deferred Windows before entering `MediaOperationStore`, continuation identity/request
revalidation, or alignment runtime/media/artifact operational access. It must return the
existing public schema with `status = failed`, `operation_id =` the durable
continuation `operation_id`, `alignment_ref = null`, and
`failure.code = alignment_runtime_unavailable`; no synthetic Alignment
`MediaOperationRecord` is created. Correcting execution alone is insufficient. No new
workflow action, status enum, platform fork, or environment-variable bypass is allowed.

Existing schema-25 tools and single-track ASR, Proxy, Draft/Review, Render,
workflow actions and `media_operation_status` remain available on Windows. This
split does not add a schema, status, error code, retry, polling path, runtime
manager or filesystem adapter; the three rejections use the existing standard
versioned error envelope and closed entry error unions.

Bootstrap compares exact schema, core, tool, and source identities when deciding whether installed code can be reused.

## Qwen credential readiness (introduced in tool schema 32)

`qwen_credential_configure`, `qwen_credential_readiness`, and
`qwen_credential_clear` are the only public entries for the current user's Qwen
Filetrans API Key and Workspace ID. They are deliberately narrow: there is no
generic secret CRUD, no credential ID, no list/read-all surface, no provider
registry, no multi-account pool, no keyring, no browser settings page and no
MCP elicitation.

Three separate entries are used instead of one action-discriminated entry
because every other domain entry in this contract is one narrow verb
(`*_read`/`*_create`/`*_update`/`*_confirm`/`*_clear`-style), and the only two
discriminated entries (`workflow_action`, `edit_change`) are finite state-machine
facades inside an existing Run/Edit-version boundary. The three credential
verbs also have three different closed inputs (`{api_key, workspace_id}`, `{}`,
`{}`) and three different effects, so one discriminated entry would need a
conditional-required-field construct that no other entry uses, would place a
secret-carrying payload in the same closed schema as a destructive clear and a
pure read, and would make `additionalProperties: false` unusable for the
argument-free actions. Splitting them also keeps each entry's error mapping a
single closed code pair instead of an overloaded one. The surface stays exactly
the three behaviours this boundary requires and nothing else.

Canonical ownership is the Roughcut user-level private configuration. The one
record lives at `<user home>/.roughcut/private/qwen-filetrans.json`: a private
`private/` directory holding one provider-specific file, separate from
`runtime.json` and from every Project. The internal format is exactly
`{ "format_version": 1, "api_key": ..., "workspace_id": ... }`. On POSIX the
directory is `0700` and the record is `0600`. On Windows the `private/`
directory is the credential confidentiality boundary: it carries a protected
DACL (`SE_DACL_PROTECTED`) whose only access-allowed ACE grants the current user
SID and inherits into children. The record and the staging file are ordinary
non-reparse files created inside that verified directory; they inherit its
current-user-only access and must not expose an additional allowed principal,
and they do not need a protected DACL of their own. The store never
reads an environment variable, so `DASHSCOPE_API_KEY` and
`DASHSCOPE_WORKSPACE_ID` remain Spike/dev/CI concepts and can never become
credential truth. Configuration publishes through a same-directory staged,
fsynced and atomically replaced file, so a failure before publication leaves any
previous valid record intact and leaves no partial file, backup or staging copy.
The `.qwen-filetrans.*.tmp` staging namespace inside that directory is reserved
to this store.

`qwen_credential_configure` creates or replaces the record. Both `api_key` and
`workspace_id` are required and both must satisfy the one shared Qwen credential
shape check. The API Key must be non-empty and must not carry any control,
format, line/paragraph-separator or surrogate character: every Unicode general
category `Cc` (C0/DEL/C1, so CR, LF, TAB, NUL, ESC, vertical tab, form feed, DEL
and NEL included), `Cf` (zero-width and bidi format characters), `Cs` (lone
surrogates), `Zl` (U+2028) and `Zp` (U+2029) is rejected. The Workspace ID must
match `[A-Za-z0-9_-]{1,64}`. That check is an injection-shape check only: it is
not a secret-strength validator, it is not a charset or length rule (printable
spacing and non-ASCII letters stay accepted), and it is not a provider
authentication test. The canonical CLI path takes
the secret off process argv: `roughcut qwen-credential-configure --json` reads
exactly one bounded closed JSON object (`{ "api_key", "workspace_id" }`) from
stdin. No `--api-key`, `--workspace-id` or other credential option exists, and
no credential is accepted through an environment variable.

`qwen_credential_readiness` is a pure local read. It never calls Qwen/DashScope,
never probes the network, never uploads audio and never validates the API Key,
so `configured` means locally configured rather than provider accepted. The
response is always non-secret and returns exactly `provider` (fixed to
`qwen_filetrans`), `status`, `workspace_id_configured`, and `next_action` when
the record cannot be used. `status` is closed to:

- `not_configured` -- no record exists;
- `configured` -- a valid record exists;
- `invalid` -- the record is malformed, has the wrong shape or types, or carries
  an unsupported internal format version;
- `insecure` -- the record or its private directory is a symlink/reparse point
  or is not a regular file/directory; the record exposes an access-allowed
  principal beyond the current user; the private directory grants access to
  anyone other than the current user or (on Windows) does not carry a protected
  DACL with inheritance for the current user; or the Roughcut user-level install
  root is a symlink/reparse point or is not a directory.

The private permission boundary is the credential's own `private/` directory:
`0700` on POSIX, and on Windows a protected current-user-only DACL that inherits
into its children. On POSIX the record file is `0600`; on Windows the record only
has to stay inside that verified directory and never expose an additional
allowed principal. `~/.roughcut` itself keeps the project's existing directory
convention because it is shared with `runtime.json`, the managed components and
the component cache, so it is checked for path safety (symlink, reparse point,
type) rather than for a private mode. An existing record with unsafe permissions
is reported as `insecure` and is never silently repaired; only an explicit
`qwen_credential_configure` rebuilds the private container and publishes a brand
new record. Neither the API Key, the Workspace ID value, nor the credential store
path is ever returned.

`qwen_credential_clear` deletes the Qwen credential record and every stale
staging copy owned by this store, keeps `~/.roughcut` and every unrelated file,
and is idempotent: clearing when the record and the staging copies are already
absent succeeds and leaves readiness at `not_configured`. After a clear no secret
backup or staging copy remains, which is why a clear still removes an owned
staging file whose credential record is already gone. A staging copy left by an
interrupted publish is cleanup residue inside the already verified private
directory, not a second credential location: readiness keeps reporting the
canonical record's own state, and either an explicit reconfigure or a clear
removes the residue.

Cloud credential readiness stays lazy and optional. `health`, `diagnostics`,
local FunASR transcription, projects without an `asr:cloud` Source, project
open, metadata edits and review/render/NLE work never check, require or report
the credential, and no existing local health/diagnostics result changes because
a credential exists. The credential is read automatically only at one boundary:
when the current explicit ASR scope contains an `asr:cloud` Source and the Agent
is preparing that Cloud transcription, the Agent first makes one local
`qwen_credential_readiness` call and reports the result. That makes readiness a
lazy local preflight at the Cloud scope and never a startup or health gate. A
never-configured credential is a setup/readiness state: it never creates a
`transcribe_source` MediaOperation, a `transcription_failed` record or a
raw-ASR result; the Agent stops, collects the API Key and Workspace ID, configures
the credential and then continues the already-confirmed Cloud scope without a
second confirmation gate. A credential that is configured but rejected by the
provider with 401/403 is a later runtime failure, not a readiness result. Cloud
execution is wired, so a configured credential continues through the same
`transcribe_source`: there is no Cloud flag, no second transcription start path
and no local FunASR fallback. The one existing media confirmation summary carries
the third-party disclosure for Cloud Sources -- the prepared audio is sent to
Alibaba Cloud Qwen for online transcription, which causes network transfer,
third-party cloud processing and corresponding cost -- and that disclosure
precedes any upload. A route marker added after a local approval changes the
Source snapshot and Project revision, so the existing stale mechanism invalidates
the old approval and the current scope must be confirmed again.

## CLI

All machine calls require `--json`. `qwen-credential-configure` additionally
requires one closed JSON object on stdin and accepts no credential option.

```text
roughcut health --json
roughcut diagnostics --json
roughcut qwen-credential-configure --json
roughcut qwen-credential-readiness --json
roughcut qwen-credential-clear --json
roughcut workflow-start --project <path> --run-id <id> --ordered-source-ids-json <ordered-string-array> --json
roughcut workflow-status --project <path> [--run-id <id>] --json
roughcut media-operation-status --project <path> --operation-id <id> --json
roughcut approve-nle-export --project <path> --run-id <id> --action-id <id> --edit-version-id <id> --expected-revision <revision> --route <fcpxml|fcp7_xml> --destination <path> [--alignment-artifact-id <id>] --json
roughcut workflow-action --project <path> --run-id <id> --action-id <id> --action <approve_scope|confirm_brief|submit_outline|approve_outline|submit_draft|approve_draft|return_to_draft|adopt_roughcut|approve_export> --input-json <closed-action-object> --json
roughcut workflow-cancel --project <path> --run-id <id> --action-id <id> --json
roughcut fake-project-roundtrip --project-name <name> --json
roughcut project-create --project <path> --name <name> [--output-preset <landscape_1080p|portrait_1080p>] --json
roughcut project-open --project <path> --json
roughcut source-add --project <path> --source <path> --import-mode <copied|linked> --expected-revision <revision> --json
roughcut person-create --project <path> --name <name> --role <role> --note <text> --expected-revision <revision> --json
roughcut source-metadata-update --project <path> --source-id <id> [--display-name <friendly-name>] --tags-json <string-array> --note <text> --expected-revision <revision> --json
roughcut speaker-map-confirm --project <path> --source-id <id> --transcript-id <id> --local-speaker-id <id> --person-id <id> --confirmed-by-user <true|false> --expected-revision <revision> --json
roughcut people-read --project <path> --json
roughcut transcribe-source --project <path> --operation-id <id> --source-id <id> --expected-revision <revision> [--speaker-diarization <true|false>] --json
roughcut transcript-page --project <path> --source-id <id> --transcript-id <id> --offset <n> --limit <1..200> --json
roughcut readable-transcript-read --project <path> --source-bindings-json <ordered-array> --expected-revision <revision> --offset <n> --limit <1..200> [--filters-json <object>] [--overlay-json <object>] --json
roughcut transcript-selection-resolve --project <path> --source-bindings-json <ordered-array> --expected-revision <revision> --view-hash <sha256> --selections-json <array> [--overlay-json <object>] --json
roughcut markdown-export --project <path> --basis <transcript|proposal|decision> --output <path.md> --expected-revision <revision> [--source-bindings-json <ordered-array> | --artifact-id <id>] --json
roughcut content-draft-create --project <path> [--parent-draft-id <id>] [--display-title <short-title>] --source-bindings-json <ordered-array> --brief-id <id> --context-hash <sha256> --blocks-json <array> --expected-revision <revision> --json
roughcut content-draft-revise-scoped --project <path> --parent-draft-id <id> --mutable-block-ids-json <string-array> --blocks-json <array> --expected-revision <revision> --json
roughcut content-draft-read --project <path> --content-draft-id <id> --json
roughcut content-draft-confirm --project <path> --content-draft-id <id> --expected-revision <revision> --json
roughcut content-draft-propose --project <path> --content-draft-id <id> --expected-revision <revision> --json
roughcut transcript-correct --project <path> --source-id <id> --parent-transcript-id <id> --corrections-json <array> --expected-revision <revision> --json
roughcut transcript-version-activate --project <path> --source-id <id> --transcript-id <id> --expected-revision <revision> --json
roughcut transcript-versions-read --project <path> --source-id <id> --json
roughcut brief-create --project <path> --theme <text> --target-duration-ticks <ticks> --focus <text> [--focus <text> ...] --allow-reorder <true|false> --expected-revision <revision> --json
roughcut brief-read --project <path> --brief-id <id> --json
roughcut agent-context --project <path> --source-id <id> --transcript-id <id> --brief-id <id> --expected-revision <revision> --offset <n> --limit <1..200> --json
roughcut multi-source-context --project <path> --source-bindings-json <ordered-array> --brief-id <id> --expected-revision <revision> --offset <n> --limit <1..200> --json
roughcut revision-context --project <path> --expected-revision <revision> --offset <n> --limit <1..200> --json
roughcut proposal-create --project <path> --source-id <id> --transcript-id <id> --brief-id <id> --context-hash <sha256> --clips-json <array> --total-duration-ticks <ticks> --expected-revision <revision> --json
roughcut multi-source-proposal-create --project <path> --source-bindings-json <ordered-array> --brief-id <id> --context-hash <sha256> --clips-json <array> --total-duration-ticks <ticks> --expected-revision <revision> --json
roughcut proposal-diff-read --project <path> --proposal-id <id> --expected-revision <revision> --json
roughcut proposal-confirm --project <path> --proposal-id <id> --expected-revision <revision> --json
roughcut multi-source-proposal-confirm --project <path> --proposal-id <id> --expected-revision <revision> --json
roughcut proposal-reject --project <path> --proposal-id <id> --expected-revision <revision> --json
roughcut decision-read --project <path> --edit-version-id <id> --json
roughcut edit-decision-read --project <path> --edit-version-id <id> --json
roughcut multi-source-edit-decision-read --project <path> --edit-version-id <id> --json
roughcut edit-change --project <path> --base-edit-version-id <id> --operation-json <object> --expected-revision <revision> --json
roughcut edit-undo --project <path> --base-edit-version-id <id> --expected-revision <revision> --json
roughcut edit-redo --project <path> --base-edit-version-id <id> --expected-revision <revision> --json
roughcut edit-history-read --project <path> --json
roughcut proxy-create --project <path> --operation-id <id> --source-id <id> --expected-revision <revision> --json
roughcut proxy-read --project <path> --source-id <id> --expected-revision <revision> --json
roughcut render-roughcut --project <path> --edit-version-id <id> --expected-revision <revision> --json
roughcut review <project-path> [--proposal-id <id> | --edit-version-id <id> | --run-id <workflow-run-id>] [--json]
```

CLI failures exit with status 2 and write one JSON object to stdout with
`ok: false` and `error.code`.

`roughcut review --run-id` reads that exact persisted WorkflowRun and routes only
`draft_review`, `roughcut_review`, or `export_review` to the existing Review
surface using its current exact bindings and artifact ref. Unsupported stages,
completed/canceled runs, stale refs, and missing or corrupt artifacts fail closed;
the legacy Proposal/Decision Review flags keep their existing behavior.

## Current contract: approved NLE export (introduced in tool schema 30; retained by 32)

`approve_nle_export` is the only public entry for writing an editable NLE
handoff. It is independent from `workflow_action(approve_export)`, which remains
MP4-only. Before calling it, the Agent must show the user the exact route/profile,
destination, adopted Decision summary, SourceAsset summary and (for multicam)
Alignment coverage, then obtain explicit approval. Core re-reads the exact
adopted Decision, current Project revision, source fingerprints/snapshots and
Alignment producer/deliverable status; the Agent does not calculate hashes or
supply a runtime/exporter override.

The input is closed and contains `project_path`, `run_id`, `action_id`,
`edit_version_id`, `expected_revision`, `route`, `destination` and
`alignment_artifact_id` (string or null). `fcpxml` always means FCPXML 1.14 and
`fcp7_xml` always means FCP7 XML/xmeml v5. A single-camera call must pass null;
a multicam call must pass the exact current Alignment ID. Current source
snapshots and the exact Decision/Alignment identity are bound into the Core
request hash and immutable handoff receipt, so changing a Decision, source,
revision, route, destination or Alignment cannot reuse the old action ID.

The application builds one immutable in-memory `NleHandoffTimeline`; writers
only serialize it. FCPXML uses primary-storyline main asset-clips and connected
auxiliary lane clips, never a direct nested spine or native `mc-clip`. FCP7 uses
ordered video/audio tracks with reciprocal A/V links. Unmapped auxiliary ranges
are absent clips/gaps; no black media, rendered MP4 or parallel MP4 is an input.
Output publication is same-directory staged, fsynced and no-replace atomic;
existing destinations, unsafe paths, stale truth and failed writes are rejected
without a success receipt. A same action ID and same exact request only reads
back the existing receipt.

This entry does not add a workflow stage, change Project/Decision/Alignment
schemas, or expose other FCPXML versions, vendor tuning, AAF, OTIO, Jianying
private drafts or Windows handoff.

## Component bootstrap

Media component installation is a separate host workflow, not an MCP tool. For
an explicit managed root and dedicated cache, use the Roughcut release
bootstrap entrypoint documented by the host installation. In a source checkout,
that is `python scripts/bootstrap.py ... --json`. Its default component action
is a read-only, network-free plan that
returns exact artifact sources, licenses, SHA-256 values, sizes, disk estimates,
external/managed reuse, FFmpeg user actions, and a stable `plan_hash`.

The Agent must show that plan and obtain explicit user approval before adding
`--apply-components --approved-plan-hash <hash>`. Apply recomputes current
state and rejects a stale hash before network or managed writes. It installs
only missing pinned managed runtime/model groups; explicit compatible external
components and verified cache artifacts are reused. Never invoke Homebrew,
winget, or another system package manager from bootstrap. Treat the returned
FFmpeg `user_action_required` as a separate approval gate.

Use `--component-health` after apply or for reuse checks. Add
`--verify-components` only for an explicit full model audit; normal health
checks the isolated runtime, lock/receipts, canonical model directories and
the runtime-required model files without rehashing all model weights.
Runtime-required files are the frozen per-model table (ASR:
`configuration.json`, `config.yaml`, `model.pt`, `tokens.json`, `seg_dict`,
`am.mvn`; VAD: `configuration.json`, `config.yaml`, `model.pt`, `am.mvn`;
PUNC: `configuration.json`, `config.yaml`, `model.pt`, `tokens.json`,
`jieba_usr_dict`; SPK: `configuration.json`, `config.yaml`,
`campplus_cn_common.bin`). A model missing any of these files is not
complete/available, even if `model.pt` exists. Scheme B cache population resolves
catalog artifacts by exact `component + filename` (`--populate-component` is optional:
a unique filename without component keeps working; an ambiguous filename without
component fails with `artifact filename is ambiguous; specify --populate-component`;
a still-ambiguous component + filename fails closed; SHA stays from the frozen catalog).
Uninstall only through the
release uninstall entrypoint (in a source checkout, `python
scripts/uninstall.py --managed-root <root> --json`); external paths and the
separate verified download cache are not uninstall targets.

`review` is the only long-running CLI command. It starts an ephemeral HTTP
server on an automatically selected `127.0.0.1` port and returns a tokenized
URL. It may be run without `--json` for direct human use. Closing the process
closes the server; it creates no daemon or temporary media. Review remains a
CLI-only lifecycle and is not exposed as an MCP daemon tool.

## stdio MCP

`roughcut-mcp` accepts one JSON-RPC 2.0 request per stdin line and writes one
JSON-RPC response per stdout line. It supports `initialize`, `tools/list`, and
`tools/call`. Process logs are reserved for stderr; the read-only component
diagnostic is an explicit versioned tool result.

The exposed tools are:

- `health` with no arguments.
- `diagnostics` with no arguments. It reports the current MCP server's read-only
  FFmpeg/ffprobe, FunASR Python and ASR/VAD/Punc/speaker model readiness plus the
  Core-derived Audalign Correlation alignment readiness object described above; it does not install,
  download or modify components. CLI health/diagnostics does not prove that a Host
  MCP has reloaded the same install root; after a package refresh the Host must
  reload/retrust and the actual MCP response must be checked. It reports no Cloud
  credential object: credential readiness is its own entry and is never a local
  health gate.
- `qwen_credential_configure` with
  `{ "api_key": "secret", "workspace_id": "id" }`. Both fields are required.
  The API Key is a secret: it is written only to the Roughcut user-level private
  credential file and is never echoed in a response, an error payload, stderr,
  stdout, a log, or an exception message.
- `qwen_credential_readiness` with no arguments. It is a pure local read of the
  stored record; it never contacts Qwen/DashScope and never validates the API
  Key against the provider.
- `qwen_credential_clear` with no arguments. It removes only the Qwen credential
  record and is idempotent.
- `workflow_start` with `{ "project_path": "path", "run_id": "id", "ordered_source_ids": ["source-id"] }`.
- `workflow_status` with `{ "project_path": "path", "run_id": "optional id" }`.
- `media_operation_status` with `{ "project_path": "path", "operation_id": "id" }`.
- `approve_nle_export` with `{ "project_path": "path", "run_id": "id", "action_id": "id", "edit_version_id": "id", "expected_revision": 1, "route": "fcpxml|fcp7_xml", "destination": "path", "alignment_artifact_id": "id or null" }`. The route fixes the profile to `roughcut_fcpxml_1_14` or `roughcut_fcp7_xml_xmeml_v5`; the nullable Alignment field must be null for single-camera and an exact current deliverable ID for multicam.
- `workflow_action` with `{ "project_path": "path", "run_id": "id", "action_id": "id", "action": "one frozen business action", "input": { "...": "the action-specific closed input" } }`.
- `workflow_cancel` with `{ "project_path": "path", "run_id": "id", "action_id": "id" }`.
- `fake_project_roundtrip` with `{ "project_name": "non-empty string" }`.
- `project_create` with `{ "project_path": "path", "name": "non-empty string", "output_preset": "optional landscape_1080p|portrait_1080p" }`. Omitting `output_preset` keeps the 1920×1080, 25 fps, 48 kHz landscape default; `portrait_1080p` creates 1080×1920 with the same frame rate and audio rate.
- `project_open` with `{ "project_path": "path" }`.
- `source_add` with `{ "project_path": "path", "source_path": "path", "import_mode": "copied|linked", "expected_revision": 0 }`.
- `person_create` with `{ "project_path": "path", "name": "name", "role": "role", "note": "", "expected_revision": 0 }`.
- `source_metadata_update` with `{ "project_path": "path", "source_id": "id", "display_name": "optional friendly name", "tags": ["tag"], "note": "", "expected_revision": 0 }`. Omitting `display_name` preserves the current name.
- `speaker_map_confirm` with `{ "project_path": "path", "source_id": "id", "transcript_version_id": "id", "local_speaker_id": "spk_0", "person_id": "id", "confirmed_by_user": true, "expected_revision": 0 }`.
- `people_read` with `{ "project_path": "path" }`.
- `transcribe_source` with required `{ "project_path": "path", "operation_id": "op_<uuid-v4-hex>", "source_id": "id", "expected_revision": 0 }` and optional `"speaker_diarization": true`; it accepts no public runtime path override.
- `transcript_page` with `{ "project_path": "path", "source_id": "id", "transcript_version_id": "id", "offset": 0, "limit": 50 }`.
- `readable_transcript_read` with an exact ordered `source_bindings` array of one or more `{ "source_id": "id", "transcript_version_id": "id" }` objects, expected revision, offset and limit; optional `filters` and Proposal/Decision `overlay` objects refine the derived view without changing paragraph identity.
- `transcript_selection_resolve` with the same bindings and expected revision, the exact `view_hash`, and a non-empty `selections` array containing paragraph IDs and, for partial selection, exact offsets, quote and occurrence.
- `markdown_export` with `{ "project_path": "path", "basis": "transcript", "output_path": "path.md", "expected_revision": 1, "source_bindings": [...] }` or a `proposal|decision` basis with `artifact_id` instead of bindings.
- `content_draft_create` with the exact ordered active `source_bindings`, active `brief_id`, matching Agent Context `context_hash`, non-empty ordered schema-2 `blocks`, current `expected_revision`, and optional `parent_draft_id` and `display_title`. Normal Agent schema-2 source-excerpt inputs MUST omit `canonical_text`; Core derives it from the exact refs. An explicitly identified legacy caller may supply the field only for strict equality validation. Schema-2 headings are independent `section_title` blocks; source and narration blocks never carry a second section-title field.
- `content_draft_revise_scoped` with the current `parent_draft_id`, a non-empty unique `mutable_block_ids` subset, one complete ordered candidate `blocks` snapshot, and current `expected_revision`. Normal Agent schema-2 source-excerpt inputs MUST omit `canonical_text`; Core performs the same exact-ref derivation, while an explicitly identified legacy caller may supply a strict-equality value. Core preserves every parent block outside that scope exactly and in relative order.
- `content_draft_read` with `{ "project_path": "path", "content_draft_id": "id" }`.
- `content_draft_confirm` with `{ "project_path": "path", "content_draft_id": "id", "expected_revision": 1 }`.
- `content_draft_propose` with `{ "project_path": "path", "content_draft_id": "id", "expected_revision": 2 }`.
- `transcript_correct` with `{ "project_path": "path", "source_id": "id", "parent_transcript_version_id": "id", "corrections": [{ "segment_id": "id", "corrected_text": "text or null" }], "expected_revision": 0 }`.
- `transcript_version_activate` with `{ "project_path": "path", "source_id": "id", "transcript_version_id": "id", "expected_revision": 0 }`.
- `transcript_versions_read` with `{ "project_path": "path", "source_id": "id" }`.
- `brief_create` with `{ "project_path": "path", "theme": "text", "target_duration_ticks": 120000, "focus": ["point"], "allow_reorder": false, "expected_revision": 0 }`.
- `brief_read` with `{ "project_path": "path", "brief_id": "id" }`.
- `agent_context` with `{ "project_path": "path", "source_id": "id", "transcript_version_id": "id", "brief_id": "id", "expected_revision": 1, "offset": 0, "limit": 50 }`.
- `multi_source_context` with an exact ordered `source_bindings` array of at least two `{ "source_id": "id", "transcript_version_id": "id" }` objects, plus the brief, expected revision, offset, and limit.
- `revision_context` with `{ "project_path": "path", "expected_revision": 1, "offset": 0, "limit": 50 }`.
- `proposal_create` with the exact context identifiers/hash, expected revision, declared total duration, and a non-empty `clips` array.
- `multi_source_proposal_create` with the exact ordered `source_bindings`, brief, schema-2 context hash, expected revision, declared total duration, and a non-empty `clips` array.
- `proposal_diff_read` with `{ "project_path": "path", "proposal_id": "id", "expected_revision": 1 }`.
- `proposal_confirm` with `{ "project_path": "path", "proposal_id": "id", "expected_revision": 1 }`.
- `multi_source_proposal_confirm` with `{ "project_path": "path", "proposal_id": "id", "expected_revision": 1 }`.
- `proposal_reject` with `{ "project_path": "path", "proposal_id": "id", "expected_revision": 1 }`.
- `decision_read` with `{ "project_path": "path", "edit_version_id": "id" }`; the closed response preserves the concrete decision type, schema version, identity, and payload.
- `edit_decision_read` with `{ "project_path": "path", "edit_version_id": "id" }`.
- `multi_source_edit_decision_read` with `{ "project_path": "path", "edit_version_id": "id" }`.
- `edit_change` with `{ "project_path": "path", "base_edit_version_id": "id", "operation": { "type": "delete|restore|reorder|trim", "...": "operation fields" }, "expected_revision": 1 }`.
- `edit_undo` with `{ "project_path": "path", "base_edit_version_id": "id", "expected_revision": 1 }`.
- `edit_redo` with `{ "project_path": "path", "base_edit_version_id": "id", "expected_revision": 1 }`.
- `edit_history_read` with `{ "project_path": "path" }`.
- `proxy_create` with required `{ "project_path": "path", "operation_id": "op_<uuid-v4-hex>", "source_id": "id", "expected_revision": 1 }`.
- `proxy_read` with `{ "project_path": "path", "source_id": "id", "expected_revision": 1 }`.
- `render_roughcut` with `{ "project_path": "path", "edit_version_id": "id", "expected_revision": 1 }`.

The four workflow tools and their hyphenated CLI equivalents call the same
finite-workflow application façade. Their action inputs and successful status
payloads are closed schemas defined by
`docs/architecture/finite-workflow-contract.md`; unknown fields, free-text
“continue” requests, plural actions, and caller-supplied approval credentials
are rejected. `allowed_actions` and `next_action` are derived results, never
stored permissions. `workflow_status` returns the common version/health fields
plus one `status` object. `workflow_start`, `workflow_action`, and
`workflow_cancel` return those common fields plus exactly `workflow_run`,
`receipt`, and `status`; `workflow_start.receipt` is null. CLI and MCP use these
same normalized field names and error codes.

`media_operation_status` and `media-operation-status` are the same pure
Project-media OperationRecord read. They accept only the safe Project path and
the already-held operation ID; there is no request payload, type selector,
retry flag, current revision, runtime, Source, run or artifact argument.
Success returns the common version/health fields plus exactly one
`media_operation` object containing the closed versioned record for the requested
operation type. The existing schema-1 types (`transcribe_source`, `proxy_create`,
`approve_export`) and schema-2 types use this single application/store path. If a pending/running writer still exists, the record
stays nonterminal; if the writer has disappeared, core re-reads under that
writer lock and converges it to `interrupted`.

This status never scans Transcript, Proxy, Render, staging, receipt or another
directory to infer success, never cleans staging/candidates, never starts or
retries a worker, and never changes Project revision, WorkflowRun stage or
`blocking_operation_ids`. A Host that loses its temporary task/session ID uses
the operation ID it already retained and makes a fresh explicit status call;
the Agent must not sleep, poll the invalid Host task, or invent progress.
`operation_not_found` and the closed `operation_*` integrity/write errors are
returned unchanged in the standard versioned error payload.

## Current contract: tracked public media starts (schema-30 surface retained by 32)

This section is normative for the current implementation. It closes the public
start path after the schema-24 pure-status phase; it retains the schema-28 media
start entries and adds the closed multicam starts below; it does not reopen media
OperationRecord phases A or B.

Before starting a synchronous ASR or Proxy CLI process or MCP call, the Host
invocation layer generates and retains the stable operation ID. Its canonical
Host-generated form is `op_` followed by the 32 lowercase hexadecimal
characters of a UUID v4. Core continues to validate the existing safe operation
ID grammar for compatible caller-supplied IDs. The Agent passes the ID through
unchanged and neither Host nor Agent computes `request_hash` or `input_hash`.
There is no ID-allocation tool and core must not first reveal the ID after a
worker has started.

The existing CLI commands and MCP tools gain these exact tracked request
fields:

```text
roughcut transcribe-source --project <path> --operation-id <id> --source-id <id> --expected-revision <revision> [--speaker-diarization <true|false>] --json
roughcut proxy-create --project <path> --operation-id <id> --source-id <id> --expected-revision <revision> --json
```

- MCP `transcribe_source` requires exactly `project_path`, `operation_id`,
  `source_id`, and `expected_revision`, with optional boolean
  `speaker_diarization` defaulting to false.
- MCP `proxy_create` requires exactly `project_path`, `operation_id`,
  `source_id`, and `expected_revision`.
- `funasr_python`, `model_root`, and `speaker_model_path` are removed from the
  tracked transcribe CLI/MCP schema. An old option, unknown field, or direct
  handler argument is rejected as `invalid_arguments` before record creation or
  worker start.
- Tracked ASR and Proxy use only the validated persistent `runtime.json`.
  Temporary arguments and `ROUGHCUT_*` process variables remain limited to
  explicit diagnostics or automated tests and cannot select runtime components
  for a public tracked start. The low-level transcription service remains an
  internal coordinator participant; no legacy or bypass public tool remains.
- `transcribe_source` selects the ASR route from the authorized Source's
  canonical `asr:cloud` marker; there is no public backend, cloud or dialect
  argument. A Cloud Source whose credential is not usable stops before any
  operation record exists and returns the same closed readiness status
  vocabulary (`not_configured` / `invalid` / `insecure`) as
  `qwen_credential_readiness`, so the Agent continues into credential setup
  instead of reporting a transcription failure. A credential that is configured
  but rejected by the provider (401/403) is a runtime failure and reads back the
  existing `asr_worker` / `run_asr_worker` / `transcription_failed` operation
  record.
- Proxy still does not require an active WorkflowRun.

The nine `workflow_action` names and their closed inputs do not change.
For `approve_export`, the existing envelope `action_id` is also the media
operation ID. The public dispatcher routes only this branch to
`run_approve_export_operation(project_path, run_id, action_id, action_input)`;
the other eight actions keep the existing façade. It must not call the
untracked export path first and add a record afterward.

All schema-25 start successes retain exactly the common
`schema_version`, `tool_schema_version`, `core_version`, `platform`, and `ok`
fields plus:

| entry | exact additional top-level fields |
| --- | --- |
| `transcribe_source` | `media_operation`, `operation_readback`, `transcript` |
| `proxy_create` | `media_operation`, `operation_readback`, `proxy` |
| `workflow_action` with `approve_export` | `media_operation`, `operation_readback`, `workflow_run`, `receipt`, `status` |

`media_operation` is the exact closed schema-1 record and
`operation_readback` is a boolean. A newly completed synchronous call sets it
to false and returns the existing domain result. A same-ID/same-request
existing operation sets it to true and starts no worker or artifact publish.
For any ASR/Proxy readback, `transcript` or `proxy` is null and the terminal
result ref in the record is authoritative. Approve-export readback uses only
the exact receipt plus the pure workflow read: `workflow_run` and `status`
remain present, while `receipt` is null until that exact receipt exists. It
never scans Render artifacts, staging, manifests, receipts by directory, or
another result tree to infer success.

Every failed schema-25 start response has exactly the common fields plus
`error`, sets `ok` to false, and makes `error` exactly
`{ "code": "<stable-code>" }`. It contains no path, stderr, token, command
line, runtime override, or free text. Missing, mistyped, or unknown request
fields return `invalid_arguments`; an unsafe operation ID or invalid closed
record returns `operation_integrity_error`; same ID with a different request
returns `operation_input_conflict`. Existing public hard-gate, workflow, and
media codes retain their responsibility and meaning.

The Host already has the operation ID if the call response is lost or the
process exits. It then makes a new explicit `media_operation_status` call with
that same ID. A preflight failure before the atomic pending record was written
correctly yields `operation_not_found`; the Host must not infer that a worker
started. The canonical Skill uses the fixed
`pending/running/succeeded/failed/interrupted` wording already defined for
status.

Implementing this section upgrades the tool schema once from 24 to 25. The
pure-status request/result semantics do not change, but its common version field
then reports 25. Schema-24 Host Packages must refresh tool discovery before
calling schema 25; there is no silent downgrade or dual dispatcher. Project,
WorkflowRun, Transcript, Proxy, Render Plan/manifest, the nine action/input
schemas, and `blocking_operation_ids` are unchanged.

The next WorkBuddy proof must be written to a new report file and separate:

1. the already-completed synthetic-record pure-status smoke; and
2. automated fixture evidence that the real public ASR, Proxy, and
   approve-export starts reached their coordinators and were readable by the
   same retained IDs.

Neither proof may use user media, download FunASR, invoke real FFmpeg, or perform
a formal Render.

## Multicam surface (schema-30 feature retained by 32; Core `0.2.8` Correlation production route)

Tool schema 29 entries above remain unchanged; schema 30 adds the closed
`source_pairs` input described at the top of this contract. The multicamera public surface is supported on the verified macOS scope. Windows M2.7 entries and automatic continuation fail closed, and natural real multi-file behavior is unverified. This section
records the one atomic multicam public surface; it does not install Audalign,
change the main Decision/Render/Workflow schemas, or add a second status/runtime
manager. The exact domain/store/operation semantics are normative in
`docs/architecture/multicam-alignment-contract.md`.

Schema 29 keeps every schema-28 entry and the nine `workflow_action` names and
closed inputs byte-for-byte unchanged, with only the additive closed
`source_pairs` field on each auxiliary alignment group:

```text
roughcut align-multicam --project <path> --operation-id <id> --alignment-id <id> --expected-revision <revision> --main-camera-json <closed-object> --auxiliary-cameras-json <closed-array> --main-audio-stable true --max-temporary-disk-bytes <n> --max-analysis-memory-bytes <n> --max-runtime-seconds <n> --json
roughcut multicam-parallel-render-prepare --project <path> --edit-version-id <id> --alignment-ref-json <closed-ref> --auxiliary-camera-ids-json <non-empty-string-array> --expected-revision <revision> --json
roughcut multicam-parallel-render-start --project <path> --operation-id <id> --prepare-ref-json <closed-ref> --json
```

`align_multicam` 的公开请求形状不变。新写入只使用持久 runtime binding 中验证通过的
`audalign==1.3.1` selection，并按 Audalign Correlation 的 44.1 kHz mono PCM16、15 秒
20/50/80 bounded serial probes（`target=aux excerpt`、`against=full main`、至少 2/3 个
non-chaining B cluster 且 diameter <=12000）执行；B 为
`0 - auxiliary_probe_start + seconds_to_ticks(delta)`。不传入 finder overrides，不做
L/R second pass、score/confidence admission、full-full Fingerprint、waveform 或 BBC fallback。
旧 Audalign Fingerprint/profile2、waveform 和 BBC operation/artifact 仍按原 ID/hash 只读回读。

- CLI `align-multicam` / MCP `align_multicam`: tracked long start with exactly
  `project_path`, Host-held `operation_id`, `alignment_id`,
  `expected_revision`, `main_camera`, `auxiliary_cameras`,
  `main_audio_stable: true`, `max_temporary_disk_bytes`,
  `max_analysis_memory_bytes`, and `max_runtime_seconds`. A camera is exactly
  `camera_id/ordered_source_ids`. The main `camera_id` must be exactly `main`;
  each auxiliary `camera_id` must match the safe ID rule and must not be `main`.
  main is non-empty and auxiliary is a non-empty array of unique groups. The
  three budget fields are explicit positive integers up to the hard maxima of
  2 GiB disk, 7,200 seconds, and 4 GiB memory; smaller requests are enforced at
  the requested size. Only existing Project Source IDs are accepted. Before
  every new start the Host must retain both a new operation ID and a new
  alignment ID; an explicit rerun may reuse neither ID.
- CLI `multicam-parallel-render-prepare` / MCP
  `multicam_parallel_render_prepare`: pure call with exactly `project_path`,
  `edit_version_id`, the exact `multicam_alignment` result ref from its
  succeeded producer record, non-empty unique
  `auxiliary_camera_ids`, and `expected_revision`. It writes nothing and
  returns exactly the common fields plus `prepare_ref` and `summary`. It accepts
  only the current `adopt_roughcut` Decision/receipt/approval chain and only
  selected `complete|partial` cameras whose current-Decision projection has
  positive mapped ticks; `omitted`, `failed`, and zero-mapped selections are
  rejected with zero writes. The recursively closed ref/summary shapes and
  prepare identity derivation are normative in the multicam architecture
  contract sections 5.1–5.3.
- CLI `multicam-parallel-render-start` / MCP
  `multicam_parallel_render_start`: tracked long start with exactly
  `project_path`, a new Host-held `operation_id`, and the unmodified
  core-owned `prepare_ref`. It must recompute the pure prepare basis before
  creating a record or worker and rejects stale or fabricated refs.

Both starts reuse the one existing `media_operation_status`; schema 30 adds no
second status, retry, cancel, polling, job, workflow action, approval token, or
runtime override. Its request is unchanged; its result union reads existing
schema-1 records and the two fixed schema-2 types below through the same
application/store path. `align_multicam` is the only alignment operation type;
parallel Render uses the separate fixed `render_multicam_parallel` media
operation and does not call or change `approve_export`.

The alignment start success adds exactly `media_operation`,
`operation_readback`, and nullable `alignment`; the parallel start success
adds exactly `media_operation`, `operation_readback`, and nullable
`parallel_render`. Existing-first same-ID/same-request readback starts no
worker and does no current preflight. Same ID/different request returns
`operation_input_conflict`. A missing operation alone may validate current
Project/Source fingerprints and persistent runtime. Transport, argument, and
preflight failures use the standard outer code-only error envelope and never
disclose Source locators, absolute paths, original filenames, stderr, commands,
or media content. A successful `media_operation_status` response may instead
contain a failed schema-2 `render_multicam_parallel` record whose nested
`error.evidence` is optional and closed: it carries only stable failure/check
identity, bounded numeric expected/actual/delta/tolerance (quota integers
`0..9007199254740991`, delta=`actual-expected`), signed-32-bit encode return
code, and a redacted stderr tail limited to 8 lines and 2048 UTF-8 bytes. A
successful partial parallel record may carry one optional singular
`result_ref.partial_failure_evidence` with the same closed bounded shape for
the first failed camera in planned order; it is limited to camera encode,
camera verify, or the fixed evidence-unavailable fallback and never describes
manifest publish. Complete-success result refs omit that field. This nested
record evidence is not an outer tool error envelope; it never contains a
command, complete stdout/stderr, media, user path, or secret.

These additive optional nested record fields do not change tool names, requests,
the common envelope, or `TOOL_SCHEMA_VERSION` 29. The existing versioned
OperationRecord readers already preserve nullable/optional nested result/error
fields and continue to read legacy four-field parallel result refs without
evidence; clients may ignore the optional evidence while still consuming the
terminal code/status.

Formal parallel output always follows: pure prepare ref, user review of camera
coverage/black-and-silence/quotas/budget, explicit approval, then start with a
new operation ID. Starting without that exact current prepare ref is forbidden.
All multicam ticks use the one Roughcut timebase of 120000 ticks per second.
`missing` is permitted only after every authorized Source in that camera has
been processed successfully and verified mappings, exact durations, and full
file bounds positively prove no coverage; absent/weak candidates or any media
processing failure are `uncertain`. Alignment artifacts directly retain their
producer operation ID and their summary counts camera-ticks.

Parallel start builds and verifies every successful camera file and its manifest
inside one operation-owned staging directory, then publishes the whole final
directory with one same-filesystem atomic no-replace move. The deterministic
parallel-render ID binds both the exact prepare ref and start operation ID; an
explicitly approved new operation ID therefore publishes a different final and
never scans, adopts, deletes, reuses, or overwrites an older final. The manifest
names the producer operation ID/type. Pure status never scans final output to
infer success, never cleans staging, and never retries or recovers a worker.
At least one selected camera must succeed before the record may succeed and a
complete or partial final may be published. If every selected camera fails, the
record fails with `parallel_render_all_cameras_failed`, has a null result, and
publishes no manifest or final. A controlled worker is the sole owner of its
ordinary failure cleanup before the terminal record; cleanup failure never
replaces an already-determined all-failed or other closed terminal cause, and
status remains read-only and performs no cleanup. Parallel manifest schema 1 and the exact schema-2 parallel
OperationRecord action/responsibility/message-code table are closed in the
multicam architecture contract sections 5.4–5.6; they do not generalize other
OperationRecord types or change the existing Render manifest.

An alignment artifact names the operation that actually published it, including
the narrow case where that record later converges to interrupted. Only a
succeeded record result ref delivers the artifact. Before either operation
publishes, any change to its frozen Project/Source/Decision/alignment/output or
runtime basis is a global failed result with zero artifact/final publication;
it is never a per-camera error.

There is no multicam Review UI; the three user interaction points remain source
grouping/authorization, alignment coverage summary, and parallel-output
approval.

## Durable workflow-scoped Multicam Setup (Batch 6 / E6 setup only)

`approve_scope` retains its historical closed input exactly:
`schema_version/confirmation_basis/source_authorizations`. A new closed input
family may add one `multicam_setup` declaration to those same fields. Omitting it
keeps the historical scope projection and input/hash bytes unchanged. The
generic CLI `workflow-action --input-json` and MCP `workflow_action` both use
the same core parser and the MCP schema advertises both closed families.

The `approve_scope` input declaration is exactly:

```json
{
  "schema_version": 1,
  "main_camera": {"camera_id": "main", "ordered_source_ids": ["..."]},
  "auxiliary_cameras": [
    {"camera_id": "aux_...", "ordered_source_ids": ["..."]}
  ],
  "source_pairs": [
    {"main_source_id": "...", "auxiliary_source_id": "..."}
  ]
}
```

All declaration fields are closed and safe-ID validated. `main_camera` is
required and non-empty; auxiliary cameras and `source_pairs` may be empty for
a durable no-aux declaration. A Source may occur in only one camera, pair
endpoints must be declared camera Sources, and pairs are persisted explicitly
rather than inferred from order, filenames, or file counts. The declaration
does not contain `project_id`, `workflow_run_id`, `asr_scope`, snapshots,
snapshot hashes, or `setup_id`; callers must not reconstruct those Core-owned
facts.

Inside the locked `approve_scope` operation, Core resolves the declaration
against the exact current Project, requested authorizations and proposed
bindings. It derives and persists the full closed `MulticamSetup` containing
Project/run identity, the exact ASR projection, existing Source snapshots in
declared camera order, the canonical snapshot hash, and canonical `setup_id`.
The persisted full object is the only shape returned by `workflow_status` and
new-process durable readback.

The setup is stored inside the active WorkflowRun and is bound to the Project,
run, current scope approval, ordered bindings, and current Source snapshots.
Changing setup facts changes the scope subject/dependency and requires the
existing pre-first-Decision scope reapproval. Repeating the exact input is the
existing action-receipt no-op. A changed referenced Source snapshot, missing or
cross-project Source, scope mismatch, malformed declaration, or stale approval fails
closed (`workflow_stale` or `workflow_subject_mismatch`) without rewriting the
stored run. A selectable Source outside the setup continues to follow the
existing scope-basis semantics. `workflow_status` and a new process/store
readback expose the exact durable setup. This Batch 6 contract does not start
post-adopt alignment or create an alignment operation; those are Batch 7.

Canonical Skill ownership is fixed: `roughcut` routes only;
`roughcut-basics` owns shared installation/status/privacy wording;
`create-roughcut` collects and echoes the exact user-authorized camera grouping
and invokes core alignment operations without deciding their result;
`revise-roughcut` remains limited to the main Draft/Decision; and
`render-roughcut` presents core-owned coverage/prepare summaries, obtains the
one explicit parallel-output approval, and echoes exact refs plus a new
operation ID. Camera eligibility, adopted-Decision validation, slots, coverage,
quotas, paths, hashes, partial/all-failed status, stale detection, and all error
codes remain exclusively in Python core. No Skill may reimplement or infer
them. This file remains the single source for all five byte-identical canonical
Skill `references/tool-contract.md` mirrors.

For `workflow_action`, `project_path`, `run_id`, `action_id`, and `action` are
envelope fields and must not be repeated in `input`. Every input below has
integer `schema_version: 1`, rejects unknown fields at every level, and uses
safe IDs. An artifact ref is exactly
`{ "artifact_id", "schema_version", "content_hash" }`; a binding is exactly
`{ "source_id", "transcript_version_id" }`; a range ref adds
`segment_id/start_ticks/end_ticks`.

- `approve_scope`: the historical input is `confirmation_basis` containing only
  the opaque `wfb_scope_…` basis ID, plus a non-empty ordered
  `source_authorizations` array. Each item is exactly
  `source_id/transcribe/speaker_diarization`; diarization requires
  transcription. For a multicam declaration, this array contains only the
  user-authorized main/content Sources. Auxiliary Sources are declared only by
  the additive `multicam_setup` and exact `source_pairs`; an auxiliary
  `source_authorizations` item is invalid even when `transcribe` is `false`.
  Core derives the full persisted setup for status/readback.
- `confirm_brief`: opaque `wfb_brief_…` `confirmation_basis`, `theme`,
  positive `target_duration_ticks`, non-empty `focus`, boolean
  `allow_reorder`, and `speaker_resolution_waivers`. Each waiver is exactly
  `source_id/transcript_version_id/local_speaker_id`.
- `submit_outline`: `title/opening/ending`, a required non-empty ordered `sections`
  array containing exactly `section_id/title/summary/target_duration_ticks`,
  `required_content_coverage` items containing exactly
  `requirement/covered/evidence_refs`, and `narration_status`.
- `approve_outline`: exact `outline_ref`.
- `submit_draft`: nullable exact `parent_draft_ref`, nullable
  `display_title`, ordered `source_bindings`, exact `brief_ref`,
  `context_hash`, complete ordered schema-2 `blocks`, and
  `scoped_mutable_block_ids`. Source-excerpt blocks contain
  `block_id/kind/refs`. Normal Agent schema-2 submissions MUST omit
  `canonical_text` and Core derives it from the exact refs. An explicitly
  identified legacy caller may supply it only as a strict-equality check against
  that same Core-derived text. Narration blocks contain exactly
  `block_id/kind/text/status/recorded_refs`; an independent heading contains
  exactly `block_id/kind/title`.
- `approve_draft`: exact `content_draft_ref`.
- `return_to_draft`: `current_subject_ref`, which is an artifact ref plus
  `kind: proposal|decision`, and exact `confirmed_content_draft_ref`.
- `adopt_roughcut`: exact `proposal_ref`.
- `approve_export`: exact core-presented `export_ref`; callers cannot submit an
  output path, FFmpeg arguments, or Proxy.

The closed `status` object has exactly `schema_version`, `workflow_run`,
`multicam_alignment`, `readiness`, `approval_statuses`, `confirmation_bases`,
`presented_subjects`, `binding_sync`, `recovery`, `transient_export_claim`,
`allowed_actions`, and `next_action`. `multicam_alignment` is Core-derived from
the adopted Decision, persisted MulticamSetup, one exact `align_multicam`
MediaOperationRecord, and the exact published alignment artifact. Before adopt
it is `null`; after adopt it is a closed view with status `not_required`,
`pending`, `running`, `succeeded`, `partial`, `failed`, or `interrupted`, and a
required auxiliary setup carries its stable `operation_id`. It never scans
latest files or infers readiness from a missing/corrupt/mismatched record or
artifact. Alignment failure does not revoke the Decision and does not block the
independent main-only export path. Callers must echo only the presented opaque
basis/ref required by the selected next action; they never calculate its hash
or treat an approval ID as authority.

The following legacy public writes are fail-closed workflow participants:
`transcribe_source`, `brief_create`, `content_draft_create`,
`content_draft_revise_scoped`, `content_draft_confirm`,
`content_draft_propose`, `proposal_create`,
`multi_source_proposal_create`, `proposal_confirm`,
`multi_source_proposal_confirm`, `proposal_reject`, and `render_roughcut`.
Without an active run they return `workflow_required`. Participant-only writes
are published only by their fixed `workflow_action`; direct calls cannot supply
a bypass flag or approval token. `transcribe_source` additionally requires the
current approved scope, exact Source binding, and explicit ASR/speaker
authorization. `proposal_reject` is limited to the current Proposal in
`roughcut_review` and does not advance the run. Formal Render is reachable only
through `approve_export`.

Read tools, Markdown export, `project_create`, Source metadata, Person and
SpeakerMap writes, Transcript correction/activation, `proxy_create`, and the
mechanical Draft/Edit change, undo, and redo operations retain their existing
rules. Opening an old Project does not create a run or infer approval.

`fake_project_roundtrip` creates and reads a temporary `project.json`, then
removes its temporary directory before returning. It never reads media or
writes a user project.

Project mutations use optimistic revision checks. `source_add` probes and
fingerprints the user-selected source. Copied imports stream into the project;
linked imports retain the resolved user path. Neither mode moves, deletes, or
overwrites the source.

`person_create` adds a minimal project Person. `source_metadata_update` atomically
updates only one source's optional friendly display name, normalized stable-order
tags, and note. Omitting the display name preserves it; changing it never renames,
moves, or overwrites the media and does not alter its locator, fingerprint, probe,
Transcript, Proxy, Proposal, or Decision. A speaker identity is
the exact `(source_id, transcript_version_id, local_speaker_id)` tuple;
`speaker_map_confirm` accepts only an existing speaker in the current active
transcript and an existing Person, with `confirmed_by_user: true`. Reconfirming
the same tuple to the same Person returns `changed: false` without incrementing
the revision; assigning another Person returns `change: "remapped"` and
increments it once. Never infer or merge identities merely because two sources
both use `spk_0`. These operations update only project metadata and never
rewrite a Timed Transcript artifact.

`transcribe_source` transcribes one authorized Source on its canonical ASR route:
without an `asr:cloud` marker it runs FunASR locally in an isolated subprocess,
and with an `asr:cloud` marker it runs the canonical Qwen Filetrans Cloud route.
The complete
raw result is written before normalization. A successful call returns transcript
metadata, not the full text. Use `transcript_page` to read stable segment IDs in
bounded pages. A normalization failure reports the preserved project-relative
`raw_result_path`; it never invents timestamps, speakers, or fine units.
The installed CLI and stdio MCP read the same schema 1 `runtime.json` beside the
Roughcut install root. Host Packages must not persist FunASR, model, FFmpeg or
ffprobe environment variables. `ROUGHCUT_*` process variables are limited to
automated tests or a user-requested one-process diagnostic override. The
schema-25 tracked start has no public runtime arguments and never treats process
variables as tracked runtime identity.
Speaker diarization is opt-in and adds only the pinned local `model_spk`
component with `return_spk_res: true` and `spk_mode: punc_segment`; the worker
never resolves a model alias or downloads weights. Provenance records whether
it was enabled, the component revision, `external|managed` source and mode,
without exposing the model path. Returned `spk_0`, `spk_1`, and later labels
remain local to that exact Source and Transcript and must be mapped to Person
only through explicit user confirmation.

`readable_transcript_read` derives a versioned, non-persistent reading view from
the exact ordered active Source/Transcript bindings. It groups only adjacent
segments with compatible resolved Person or local-speaker identity and never
crosses a Source or Transcript boundary. Paragraph IDs and the page-independent
`view_hash` are deterministic; filtering by Source, Person, adoption state or
keyword and bounded pagination do not alter paragraph identity or source order.
Every unfiltered segment appears exactly once. The view returns exact segment
IDs and ticks, friendly Source metadata and dynamic Person resolution, but never
locators, fingerprints, raw-ASR/model paths or temporary directories. An
optional immutable Proposal/Decision overlay marks paragraphs as adopted,
partial or unadopted without changing Transcript artifacts.

`transcript_selection_resolve` binds a human selection back to that exact view.
Its `start_offset` and `end_offset` are zero-based, half-open Unicode code-point
offsets, not UTF-8 byte offsets or JavaScript/DOM UTF-16 code units. Browser
clients must convert UTF-16 selection indices before calling the tool.
Whole segments always resolve to their complete effective text. A partial
selection receives narrower ticks only when complete, ordered, non-overlapping
fine units align uniquely to the effective text after ignoring only Unicode
punctuation and display whitespace; spoken Han characters, letters, numbers,
emoji and token order must still match exactly. Display-only punctuation never
creates an independent timestamp or cut boundary. Otherwise the selection
explicitly expands to the containing full segment. It never interpolates
timestamps, silently chooses the first repeated phrase, skips intervening words
or joins discontinuous text. Partial selection across paragraphs is rejected by
this JSON tool; complete paragraph selections may span paragraphs and are
returned in view order.

`markdown_export` writes a UTF-8 Readable Transcript or an existing immutable
Proposal/Decision script together with a sibling `.map.json`. The map binds the
view or artifact hash to exact Source, Transcript, segment and half-open tick
references. Both files are deterministic and path-free, are published as one
atomic pair, and are export-only: Markdown is never imported over the project.
Content Draft export is not part of this tool schema.

`content_draft_create` stores an immutable, unconfirmed schema-2 Content Draft
between an active Brief and a Proposal. It freezes the current revision, Brief,
exact ordered Source/Transcript bindings and matching Agent Context hash, but
does not mutate the Project, increment its revision, or change
`active_content_draft_id`. Its ordered `blocks` contain source excerpts,
narration, and independent `section_title` heading blocks. A heading has only
`block_id/kind/title`: it creates no clip, duration, sync sound, or media
placeholder. Headings may be consecutive or at the end (empty chapters), and
an untitled preface is valid; equal titles remain distinct by heading identity.
There is one schema-2 section truth, never embedded `section_title` fields on
source or narration blocks.

Schema-1 artifacts remain immutable and `content_draft_read` returns their
original schema. At the first successful child-producing edit, core projects
each legacy embedded title into an independent heading immediately before its
block. Its ID is the lower-case first 32 hex characters of
`SHA-256(UTF-8(content_draft_id) || NUL || UTF-8(block_id) || NUL || ASCII(index))`
with the `legacy_section_` prefix. Any identity collision fails closed; no
alias or overwrite is permitted. The projection and child publication are one
atomic write, so failure creates no child. `content_draft_revise_scoped`,
`workflow_action` `submit_draft`, Review body/narration/section edits, and
`content_draft_confirm` all publish schema-2 children; `content_draft_create`
also writes schema 2 whether or not it has a parent. A source-excerpt block
contains exact W2 refs and core-derived canonical text; normal Agent
create/revise/submit inputs MUST omit `canonical_text`, and Core derives it
through the existing exact Transcript range path. Only an explicitly identified
legacy caller may supply `canonical_text`, and Core requires exact equality; it
never accepts Agent-recomputed or rewritten text. Schema-2 read output
may also contain optional `display_text`. Equal display and canonical text is
omitted. Public Agent create/revise/submit inputs must not provide
`display_text`; for inherited source blocks core preserves only the uniquely
validated parent display punctuation. Caller text is only an equality check, so
rewrite, omission, discontinuous selection and incorrect ticks are rejected. A
narration block is explicit editorial text with `draft`,
`approved`, or `recorded` status and is never represented as source speech
unless `recorded_refs` bind one exact active Transcript whose canonical
effective text matches exactly. Recognition differences must first be handled
through immutable Transcript correction.

`content_draft_revise_scoped` is the additive default for Agent local revisions.
It requires a current parent and a complete candidate snapshot, but only permits
the declared existing block IDs to change, move, or disappear. Every undeclared
parent block must remain field-for-field equal and in the same relative order;
new block IDs may be added for replacement content. Success creates one
immutable unconfirmed child without changing Project revision or active state
and returns changed/unchanged block IDs plus before/after/delta media duration.
Validation or save failure leaves no child artifact. An explicitly requested
whole-draft redesign continues to use `content_draft_create` with a parent ID.

`content_draft_confirm` is a separate explicit user gate. It creates an
immutable confirmed child whose parent is the candidate, promotes any remaining
draft narration to approved, and atomically activates that child while
incrementing Project revision once. Candidate and historical confirmed files
remain unchanged. Reconfirming the already active confirmed child is an
idempotent no-op. `content_draft_read` reports `current` or `stale` with explicit
reasons when revision, Brief, bindings, active Transcript, context, or active
Content Draft state differs.

In addition, `content_draft_read` returns the additive `duration_acceptance`
object with `target_duration_ticks`, `actual_duration_ticks`, `delta_ticks`,
`status`, `tolerance_ticks`, and `accepted_upper_bound_ticks`. The actual value
is derived from the current candidate's exact source refs and recorded narration
refs through the existing duration truth; headings and unrecorded narration do
not contribute. `delta_ticks` is `actual - target`, and the default inclusive
accepted interval is `target .. target + floor(target * 10 / 100)`. Status is one
of `within_target`, `under_target`, or `over_target`. This is a readback/warning,
not a new Brief tolerance field or a blanket rejection; stale/ref changes cause
the service to derive it again for the candidate being read. The Core warning is
not permission for a Skill to silently approve or adopt an `under_target` or
`over_target` candidate: the canonical Skill prefers a revise, retaining one
requires the user's explicit acceptance, and a requirement for `within_target`
is an acceptance stop until it is met.

`content_draft_propose` accepts schema 1 or schema 2 after exact-ref validation,
but only the active, current, user-confirmed draft. Independent heading blocks
are ignored and never become Proposal items, clips, duration, or placeholders.
Every narration block must already be recorded; unrecorded narration blocks the
operation and never becomes a placeholder clip, fabricated timing, or omitted
content. The service compiles source/narration block/ref order through the
existing Proposal application service with canonical refs. Proposal, manuscript,
and subtitle display prefers validated source `display_text`; clips, refs, ticks,
duration, order, and media identity remain derived only from exact canonical
refs. A multi-ref source block is split once at its fixed connection boundaries,
so boundary punctuation is not duplicated or randomly assigned. It does not change
Project revision, confirm the Proposal, create a Decision, or authorize Render.
Before the first Decision, bindings may expand through a new draft and fresh
context; after a Decision exists, they must exactly match its frozen binding
scope or the caller must start a separate rebaseline task.

Review direct manipulation is a private schema-2 endpoint, not a new public
workflow action. Its local punctuation editor changes only validated display
punctuation; it never changes exact refs/ticks. `move_selection` moves a resolved draft selection and
`insert_source_refs` copies exact source refs; both envelopes carry
`operation_id`, expected checkpoint/current candidate refs, the source
candidate/hash, and a target containing only `paragraph_id`, `block_id`, and a
UTF-16 offset. Pointer movement is local-only. On drop, the server resolves
the target and exact refs under one Project lock, then performs CAS,
prepare/publish, and checkpoint advancement. A stale, illegal, internal,
duplicate, out-of-order, or failed drop writes nothing. Narration selection
snaps any non-empty range within one narration block to the complete block;
cross-narration and narration/body mixtures are illegal, and text, status,
placeholder, and recorded refs move together.

The private section operation set is closed to `section_reorder`,
`section_rename`, `section_split`, `section_merge`, and `section_delete`.
Payloads identify heading block IDs and exact boundaries; split heading IDs
are generated by core. Merge removes only the selected heading, preserving all
body and narration blocks. Delete uses the existing undoable semantics,
including deleting an empty heading without a danger confirmation. Body and
section edits share one Draft Workspace checkpoint, CAS, operation history,
and undo/redo; each successful operation creates exactly one immutable child.

`transcript_correct` accepts a non-empty correction array only for the exact
active parent Transcript. A non-null `corrected_text` is trimmed and must remain
non-empty; `null` restores display of `original_text`. A changed request creates
and activates one immutable child, preserves every segment ID, order, timing,
original text, fine unit, local speaker, confidence, editorial mark, and ASR
provenance, and increments the Project revision once. The parent file is never
rewritten. Confirmed Speaker Maps are inherited only from the exact
`(source_id, parent_transcript_version_id, local_speaker_id)` keys to the child;
no other source or Transcript is inferred. An effective no-op returns
`changed: false` without creating an artifact or changing the revision.

`transcript_version_activate` validates that the immutable artifact belongs to
the source and increments the revision only when the active version changes. It
does not rewrite Transcripts, Speaker Maps, Proposals, Decisions, Briefs, or
clips. `transcript_versions_read` returns bounded version metadata rather than
the full text, validates parent ownership and obvious missing/self/cyclic
chains, and reports the active Edit reference as `none`, `current`, or `stale`.
For both schema-1 and schema-2 Decisions, stale mismatches contain the source,
frozen referenced Transcript ID, and current active Transcript ID. The frozen
Edit remains immutable; reactivating its referenced version can make the status
current again without migration.

An Edit Brief contains only a theme, a positive target duration in ticks,
non-empty focus points, and an explicit `allow_reorder` boolean. `brief_create`
stores an immutable brief, activates it, and increments the project revision.
`agent_context` requires the exact current revision, active brief, active
transcript version, and source. It returns bounded stable-ID pages, safe source
metadata including tags/note, project Person summaries, only that transcript's
confirmed speaker maps, project settings, allowed operations, and one
page-independent `context_hash`. Each segment retains `local_speaker_id` and
adds resolved `person_id`/`person_name`; unresolved speakers remain local and
are never inferred. The hash covers Person, source metadata, and speaker-map
state. It never returns media, absolute paths, fingerprints, raw ASR paths, or
internal temporary locations. This schema-1 entry remains available for
existing single-source callers.

`multi_source_context` is the additive schema-2 context entry. The caller must
bind at least two distinct sources to their exact active Transcript versions in
an explicit order. It returns source summaries in that order, project Person
summaries, only the relevant confirmed speaker maps, and one flattened bounded
segment page. Each segment carries its own `source_id`,
`transcript_version_id`, `segment_id`, `local_speaker_id`, and dynamically
resolved `person_id`/`person_name`. Equal local speaker or segment IDs from
different sources remain separate. Every page shares one hash covering the
exact ordered bindings, revision, Brief, Persons, relevant maps, source
tags/notes, and complete bound Transcript contents. Changing binding order or
any covered state invalidates the hash. The response never exposes locators,
fingerprints, absolute or raw-ASR paths, temporary directories, or media.

`revision_context` is the bounded entry for continuing from the current active
Decision. The caller supplies only the project, current revision, and page
bounds; the service reads the active Decision itself and returns its exact
schema, base Edit ID, complete ordered base clips and duration, exact ordered
source/Transcript bindings, current active Brief, allowed operations, safe
source/Person/Speaker Map summaries, and paged Transcript segments. Schema 1 is
represented by one exact binding without changing the schema-1 Proposal
contract. Every page shares the same base Edit, bindings, Brief, base clips,
revision, and the existing Agent Context `context_hash`; no second overlapping
hash is introduced. The response never contains project paths, locators,
fingerprints, raw-ASR paths, branch file locations, provider/model parameters,
or media. Missing active Decision/Brief, stale revision or Transcript bindings,
invalid pagination, and corrupt or unsupported Decisions fail explicitly.

`proposal_create` accepts only a complete ordered snapshot. Every clip contains
`clip_id`, `source_id`, `transcript_version_id`, `segment_id`, half-open
`source_in_ticks`/`source_out_ticks`, `reason`, and `display_text`. Validation
rejects stale context or project state, unknown IDs, duplicate clip IDs,
invalid or out-of-segment ranges, unauthorized reordering, incorrect totals,
and unreasonable target overruns. For newly created schema-1 and schema-2
Proposals, core derives canonical `display_text` from the exact segment/range:
a full range is the full effective text, while a partial range is allowed only
at consecutive trusted fine-unit boundaries. Caller text is consistency input,
not truth; rewrites, omitted middle words, discontinuous selections and shorter
text over a wider audio range are rejected. Existing Proposal and Decision
artifacts retain their stored text and remain readable without migration. A
proposal artifact does not change the active edit or project revision.

`multi_source_proposal_create` stores a distinct schema-2 Proposal and freezes
the complete ordered source/Transcript bindings, Brief, context hash, and clip
snapshot. Each clip is independently checked against its bound source, active
Transcript, real segment, and source duration. Unknown or crossed identities,
duplicate clip IDs, empty or out-of-bounds half-open ranges, incorrect totals,
and stale revision/hash/Brief/Transcript state are rejected. When the Brief has
`allow_reorder: false`, order is the deterministic Project source order followed
by Transcript segment order, so an A-to-B-to-A sequence is rejected. With
`allow_reorder: true`, a valid A-to-B-to-A sequence is allowed. Creation does
not change the revision; only `multi_source_proposal_confirm` creates its
immutable schema-2 Decision and increments the revision once. Use
`multi_source_edit_decision_read` for readback.

When an active Decision exists, both Proposal creation tools become
base-aware. A revision Proposal must use the active Decision's same schema and
exact source/Transcript binding scope; schema 2 cannot add, remove, replace, or
reorder bindings. Its `base_edit_version_id` is the current active Edit.
Reusing a current clip ID preserves its source, Transcript, and segment
identity; only its half-open range, display text, and reason may change after
normal segment/Brief validation. A newly selected real Transcript segment uses
a new safe clip ID. Creation remains read-only with respect to Project revision,
active Edit, and redo. Confirmation repeats the schema, binding, identity,
revision, Brief, context, and Transcript checks before creating one child
Decision and clearing redo. With no active Edit, the existing initial schema-1
and schema-2 Proposal behavior remains unchanged.

`proposal_diff_read` accepts only a Proposal whose base is the current active
Decision and whose project revision, context hash, current Brief, exact
bindings, Transcript contents, Persons, Speaker Maps, and source metadata are
still current. It returns a deterministic structural diff: base/candidate IDs
and schema, before/after clip counts and durations, duration delta, candidate-
ordered additions, base-ordered removals, candidate-ordered field changes,
shared-clip reorder status, and complete before/after clip-ID order. Changed
fields are limited to `source_in_ticks`, `source_out_ticks`, `display_text`, and
`reason`; each entry contains the exact before/after clip. Adding or removing a
clip alone does not imply reorder. Diff reading creates no file and changes no
revision, active Edit, or redo state. Any Project mutation after Proposal
creation makes diff and confirmation stale.

Legacy schema-1 Proposals and Decisions remain readable and continue to derive
the original single-source Virtual Timeline. A schema-2 Decision derives a
continuous mixed-source Virtual Timeline whose lookup is clip-specific,
including when a later clip returns to an earlier source. Review now dispatches
schema 1 and schema 2 explicitly. Schema-2 Proposals and Decisions may be used
for local virtual Preview, but remain invalid inputs to the schema-1 formal
renderer.

`edit_change` is only for a delete, restore, complete reorder, or half-open trim
that the user has explicitly requested. The Agent must not use it for an
autonomous compression, recovery, or reorganization. Every call supplies the
exact current revision and active `base_edit_version_id`. A changed operation
creates one full immutable Decision whose parent is that base, increments the
revision once, and clears redo; no-op reorder/trim creates nothing. Restore may
only recover the nearest matching complete clip from the active ancestor chain.
`edit_undo` and `edit_redo` only move the active pointer along the validated
persistent redo chain and never create, rewrite, or delete Decisions.
`edit_history_read` returns path-free active ancestry, redo IDs, restorable clip
summaries, current clips, and total duration. Broken, missing, cyclic,
cross-schema, or discontinuous history fails explicitly rather than being
repaired. Confirming a new Proposal after undo also clears redo while retaining
the abandoned immutable branch files.

Use the direct M2.3 edit tools only for a user's exact operation, such as
“删除 `clip_123`”“撤销一次” or “把 `clip_a` 放到 `clip_b` 前”. A semantic request
such as “压缩到两分钟”“恢复关于招生的内容” or “把嘉宾结论前置” must follow:
`revision_context` → one complete Proposal → `proposal_diff_read` → show the
complete candidate and diff → wait for explicit user confirmation → the
existing schema-matched confirm tool. Never decompose semantic revision into
unconfirmed `edit_change` calls. Do not automatically remove filler/pauses,
infer recording commands, or freely rewrite spoken text. If the user changes
theme, target duration, focus, or `allow_reorder`, first obtain explicit intent
and create a new Brief, then read a fresh revision context.

The Agent must show clip source, time range, order, reason, and total duration
before asking the user to confirm. Only `proposal_confirm` creates an immutable
Edit Decision, makes it active, and increments the revision. `proposal_reject`,
stale confirmation, repeated confirmation, and failed confirmation never create
or overwrite a decision. Use `decision_read` for unified confirmation readback;
the schema-specific readers remain available for legacy callers.

The review page reads the same complete Proposal/Decision snapshot and the
server-derived virtual timeline. Deletion and reorder remain an uncommitted
full-clips draft until the page calls the existing Proposal application service;
only a separate explicit confirmation invokes the existing confirmation gate.
For schema 2, the snapshot preserves the exact ordered source/Transcript
bindings, resolves confirmed People dynamically, and exposes one media URL per
bound source. Virtual playback switches source at each clip boundary, including
an A-to-B-to-A return, without creating a proxy or preview file. Media requests
are limited to source IDs authorized by that snapshot and retain byte Range
support.
The server enforces the current revision/context, loopback Host and Origin,
short-lived session token, registered-source media allowlist, path containment,
and byte Range requests. Unsupported browser media is reported without proxy
generation or transcoding. Installed review runtime uses packaged static assets
and never invokes Node or npm.

`proxy_create` creates or reuses a derived project-local playback proxy for one
registered source without changing Project revision or source truth. Profile 1
is a software-encoded H.264/yuv420p faststart MP4 at the Project frame rate on
an even Project-aspect canvas no larger than 1280×720, with no source-content
upscale. It uses AAC-LC 48 kHz stereo when the source has audio, a black canvas
for audio-only input, and no fabricated audio for silent video. Rotation is
baked and VFR/non-zero source timestamps are normalized to a zero-based CFR
timeline. HDR/PQ/HLG is explicitly unsupported in this stage.

The proxy cache key binds the versioned profile, source fingerprint, relevant
probe facts, derived canvas, Project frame rate, and fixed encoding settings;
it excludes paths, Project revision and editorial metadata, tool locations and
ordinary tool-version strings. Only a fully verified ready manifest is reusable.
`proxy_read` reports `missing`, `ready`, `stale`, or `invalid` with a stable
reason and sanitized project-relative paths. A ready hit performs read-only
fingerprint/ffprobe validation, does not invoke FFmpeg transcoding, and does not
rewrite either file. Failed, timed-out, interrupted, or concurrently stale
creation removes only its unique candidate and never damages an older ready
cache. Proxy artifacts are derived playback media: they do not replace the
registered source and are not formal render inputs. This stage does not wire
proxies into Review.

Proxy argument failures use `invalid_arguments`; operational, cancellation,
and unsupported-media failures use `proxy_operation_failed`, `proxy_cancelled`,
and `proxy_unsupported` respectively. Responses never expose source locators,
absolute paths, full FFmpeg commands, or temporary paths.

`render_roughcut` accepts only the exact active immutable Edit Decision and
current project revision. The Agent must first show the ordered clips, source
time ranges, total duration, and output settings, then call it only after the
user explicitly requests the formal MP4. It dispatches on the Decision schema:
schema 1 retains the single-source Render Plan schema 1 path; schema 2 freezes
Render Plan schema 2 with the Decision's ordered source/transcript bindings,
complete registered original Source snapshots and fingerprints, and exact
A→B→A-capable clip order. Stale revisions, Decisions, Transcript bindings,
changed sources, unknown sources, and invalid clip bounds fail.

Rendering uses FFmpeg only with registered original source locators and never
with Proxy or Review media. Every clip has an input-side accurate seek and
bounded duration against its own source. Video is rotated, scaled and padded;
audio-only clips receive black video. If any rendered clip has audio, silent
video clips receive quota-matched silence and the result uses AAC stereo at the
project sample rate; if every rendered clip is silent video, the MP4 has no
audio track. Output is H.264 with project dimensions and frame rate, square
pixels, `yuv420p`, and faststart. The tool verifies container, codecs, settings,
zero start, an exact global frame quota and an exact schedule sample quota, a
real AAC timeline/decoded sample boundary within one AAC frame (<= 1024 samples
at the project sample rate) of the schedule sample count, duration tolerance,
non-empty output, faststart and a full decode before publishing. Failure or
cancellation publishes no MP4 or manifest.

A successful payload includes a render ID, project-relative MP4 and manifest
paths, and acceptance checks. Single-source rendering retains manifest schema 2.
Schema 2 Decisions produce manifest schema 3 with ordered `input_sources`,
`source_bindings`, exact source/tick clips, tool versions, output settings and
the versioned Render Schedule. Schedule entries contain cumulative timeline
ticks, globally quantized half-open frame/sample quotas, and bounded clip-local
input windows. The manifest also records sanitized command/filter hashes and
strategy summary, render/verification wall times, actual output frame and audio
sample boundaries, sanitized probe facts, and verification results. It contains
no locator, Proxy path, token, absolute media path, transcript text or raw ASR
content.
Do not invoke FFmpeg directly or edit the immutable Render Plan or manifest.
