# Roughcut technical specification

## Architecture

The Python core is the authority for project state, validation, media operations, and versioned JSON CLI/MCP responses. Skills orchestrate the workflow; Host Integration only configures discovery and transport. The local Review server serves the draft editor and preview on loopback. The installed core ships built static assets and does not require Node.

Projects use immutable, traceable Timed Transcripts, Content Drafts, Proposals, Decisions, and Render Plans. Source media is read only. New transcript corrections create a version; spoken source excerpts use core validated exact refs, never rewritten dialogue. Relevant revisions invalidate previous approvals. The public tool schema and command parameters are specified in [Agent tool contract](agent-tool-contract.md).

## Workflow and confirmation

Source discovery is read only. The Agent reports file scope, time, disk estimate, and optional cloud transfer before the user approves preparation. The user confirms editorial requirements and any Agent proposed or materially changed outline. A Draft can be edited in the Review UI or revised through the Agent. Adoption binds the current preview and exact edit decision. Render preparation and start require the current approval. Stale or ambiguous references fail closed.

When a user explicitly requires content order, the deterministic [content order contract](architecture/content-order-contract.md) applies. 没有用户明确顺序要求时结果为 `NOT_APPLICABLE`；no order constraint is inferred from section order or `allow_reorder`.

## Media and platform

Local FunASR/Paraformer is the default transcription route. An explicit `asr:cloud` source tag selects optional Qwen Cloud Filetrans, subject to a scope disclosure and user level private credential readiness. Cloud credentials and signed URLs must not enter project records, logs, or normal tool responses. `health` and `diagnostics` do not require cloud credentials. FFmpeg and the approved component catalog govern local media operations; installed component selection comes from Roughcut's persistent runtime binding.

The main edit uses source time ranges and project frame time base. Rendering produces an H.264/AAC MP4 with a manifest. Optional multicamera audio alignment keeps the main recording as the sole content and cut authority; auxiliary outputs preserve matching timeline length, use black/silence for gaps, and do not switch cameras. This path is supported on the verified macOS scope; Windows multicamera support is unverified.

NLE handoff uses adopted decisions and original source assets. FCPXML and FCP7 XML exporters derive from the same timeline projection. Compatibility is specific to the target application and version; see [NLE contract](architecture/nle-handoff-contract.md). NLE handoff does not replace the MP4 export approval.

## Data and safety contracts

Current detailed operation invariants live in the [finite workflow](architecture/finite-workflow-contract.md), [media operation](architecture/media-operation-record-contract.md), [installation operation](architecture/installation-operation-record-contract.md), [multicamera alignment](architecture/multicam-alignment-contract.md), [draft editor](architecture/draft-editor-direct-manipulation-contract.md), and [draft workspace](architecture/draft-workspace-checkpoint-contract.md) contracts. Executable vectors are in `core/tests/fixtures/`. Core tests and tool schemas are published alongside the code.

Review and media endpoints bind to a project, revision, token, Host, Origin, and loopback server. Render and transcription operations must preserve source immutability and report typed failure responsibility. No source path, secret, signed URL, or raw private media belongs in package metadata or test snapshots.
