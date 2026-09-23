# Roughcut user workflow

1. The user provides source files or camera folders. The Agent enumerates and probes them read only, reports scope and cost, and asks for one explicit approval before media preparation. Camera roles and main sound source are user decisions.
2. The Agent checks the installed runtime binding, runs approved transcription, reports coverage and uncertain speech, and helps identify speakers. Transcript corrections create a new immutable version without changing source timing.
3. The user supplies an editing goal and duration. The Agent reads the authorized transcript, proposes an outline when needed, and obtains approval for a proposed or materially changed structure. Explicit content order uses the [content order contract](architecture/content-order-contract.md); 没有用户明确顺序要求时结果为 `NOT_APPLICABLE`。
4. The Agent builds a Content Draft using exact source refs and explicit narration blocks. The user edits text selection and block order in the local Review UI or asks the Agent for further revisions. Unrecorded narration must become a bound source before it can be rendered.
5. The user reviews the continuous rough cut preview and adopts the current candidate. Returning to the draft makes a new candidate and requires a fresh adoption.
6. The user approves formal export. Roughcut renders the main MP4 and, when eligible and approved, auxiliary camera outputs or an editable NLE handoff. The Agent reports outputs and failures against the exact operation record.

A stale approval, unresolved source ref, ambiguous edit target, missing component, or changed source scope stops the affected action. The Agent asks for a new decision or reports the responsible failing component. It does not guess or reuse an old confirmation.

Optional Qwen cloud transcription is selected only for explicitly marked `asr:cloud` sources. The source confirmation includes third party upload and cost disclosure. Credential readiness is checked only when preparing this route. Local transcription does not need Qwen credentials.

The Review UI supports direct draft edits, continuous preview, and adoption; it is not a full video editor. For CLI/MCP details see [Agent tool contract](agent-tool-contract.md), and for setup see [installation](installation.md).
