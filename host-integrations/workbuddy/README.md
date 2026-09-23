# WorkBuddy Host Integration

This directory contains only the WorkBuddy-specific MCP template and installation notes.
The Host Package is generated from the canonical workflows in `agent-skill/skills/`; do not
copy or maintain a second workflow source here.

WorkBuddy uses MCP + all 5 canonical Roughcut Skills as one standard integration:
`roughcut`, `roughcut-basics`, `create-roughcut`, `revise-roughcut`, `render-roughcut`.
Skills are an automatic standard item, installed together with the MCP server.

Do not run Roughcut state-mutating CLI from a WorkBuddy Desktop shell that injects the
safe-delete shim. Production mutations must use the configured stdio MCP server, which is
isolated from the shell shim. WorkBuddy Desktop may replace `os.unlink` /
`pathlib.Path.unlink` with move-to-trash semantics, which breaks Roughcut's single-link
transaction publication.

The integration was scoped against WorkBuddy 5.2.6.0 on Windows 11 x64. That host exposes a
user-level stdio MCP configuration at `%USERPROFILE%\.workbuddy\mcp.json` and accepts an
absolute executable path as the server command. Build a temporary package with the exact
Roughcut environment selected for that machine:

```text
python scripts/build_host_package.py --host workbuddy --output <temporary-output> --mcp-command <absolute-path-to-roughcut-mcp.exe>
```

Component paths do not belong in this Host Package. Bootstrap must publish the approved
external/managed selection to Roughcut's versioned persistent runtime binding before this
package is generated. CLI and MCP read that same binding, so a CLI fallback cannot produce a
different component diagnosis. Host-specific environment variables are temporary diagnostic
overrides only and must not be used as installed component state.

Merge the generated `.mcp.json` entry into WorkBuddy's existing MCP configuration; never
replace unrelated user servers. Install all five generated `skills/` directories through the Skill
discovery location confirmed by the running WorkBuddy version. Trust the local server, reload
the WorkBuddy session, and verify native discovery of all five generated Skills plus the `health`,
`fake_project_roundtrip`, and `media_operation_status` tools. Building the Host Package alone
does not complete the installation. If a Host task ID disappears after a
media operation ID was returned, retain that operation ID and call `media_operation_status` once;
do not sleep, poll the invalid task, scan artifacts, or infer success. Remove the temporary
generated package after verification.

When WorkBuddy exposes the configured Roughcut MCP server, Project mutations and tracked media
starts must use its native MCP tools. A native schema-validation or tool-call failure is a Host
compatibility failure and must stop the task; it does not authorize a shell CLI fallback.

The Host Package does not contain the Python core, media components, models, or business rules.
