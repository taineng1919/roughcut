# Roughcut product intent

Roughcut helps an editor turn speech led source media into an accountable content draft and a playable rough cut. It serves interviews, talking head recordings, lectures, livestreams, and short projects where spoken material carries the main story.

The user defines the source scope and editing goal, reviews the outline and draft, and adopts the rough cut. The Agent proposes and revises structure, but every source excerpt must resolve to the original transcript and time range. User approval is bound to the current candidate and cannot be reused after relevant changes. Original media stays intact.

The normal path is: confirm sources and media costs; prepare transcription; review speakers and transcript coverage; agree on a brief and outline; build a draft from exact source excerpts and explicit narration; edit the draft; preview and adopt a rough cut; approve export. Optional narration must be recorded and bound as media before it can be cut. A user can return from preview to revise the draft.

The current output is an H.264 MP4 rough cut. Supported macOS setups can align auxiliary cameras by audio and render parallel files without selecting cameras or mixing audio. NLE handoff can reference original media through FCPXML or FCP7 XML; usability depends on the target software version. These paths do not imply finished picture or sound editing.

Roughcut does not promise automated visual storytelling, B-roll selection, camera switching, captions, titles, transitions, color grading, or final mixing. The local Review UI is for draft editing and preview, not a full nonlinear editor. The Python core, project records, and JSON CLI/MCP remain host independent; Skills orchestrate the process and host packages only configure access.

For exact data and operation interfaces, see [the technical specification](../spec.md), [Agent tool contract](../agent-tool-contract.md), and current [architecture contracts](../architecture/).
