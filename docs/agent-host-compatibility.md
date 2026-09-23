# Agent host compatibility

Roughcut exposes one Python core through versioned JSON CLI and local stdio MCP. Canonical Skills in `agent-skill/` describe orchestration; generated host packages add discovery and configuration without copying core rules.

| Host | Integration | Scope |
| --- | --- | --- |
| Codex | Generated Skills and MCP/CLI host package | Primary supported host path |
| WorkBuddy | Generated Skills and MCP/CLI host package | Supported host path; refresh and trust depend on host version |
| Claude Code and other local agents | Direct CLI or MCP integration | No dedicated package compatibility claim |
| Chat only agents | No local tools | Unsupported |

A host must support local process execution and access only to user authorized project and media paths. The Review server uses loopback, project and revision binding, and token/Host/Origin checks. Host configuration does not own Qwen credentials; Roughcut's user level private credential store does. Installation and host setup steps are in [installation](installation.md).

Platform support is scoped to the specific operation. General core and CLI tests run on macOS and Windows. Multicamera alignment and parallel output have a supported macOS scope; Windows support for that feature has not been verified. NLE import success depends on format, target application, and version.
