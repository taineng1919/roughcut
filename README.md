# Roughcut

Roughcut is a local, agent driven tool for assembling transcript based video rough cuts. It is intended for interviews, talking head recordings, lectures, and other material whose structure comes mainly from speech. The Python core owns the project data and media operations; an Agent uses the versioned JSON CLI or MCP tools and the shared Skills to guide the user. A local Review UI supports draft editing and rough cut preview.

Roughcut can import linked or copied sources, transcribe audio with local FunASR, organize exact source excerpts into a draft, preview an edit, and render an H.264 MP4. Optional Qwen cloud transcription requires an explicit source marker, credentials, and consent for upload and cost. On supported macOS setups, audio alignment can produce parallel auxiliary camera rough cuts. Editable NLE handoff is available through FCPXML and FCP7 XML with compatibility dependent on the target application and version.

It does not automatically create a finished program with titles, music, effects, mixing, or camera switching. Windows multicamera alignment has not been verified. A successful project still needs the user's editorial decisions and review.

## Install or update with an Agent

Give your terminal-capable Agent this repository URL and the [installation and update guide](docs/installation.md).

## Run from source

Python 3.11 or newer is required. From this checkout:

```sh
python3.11 -m pip install -e "core[dev]"
roughcut health --json
roughcut diagnostics --json
```

Media components are managed separately. Read [installation](docs/installation.md) before running transcription or rendering. The source checkout and release builder exist, but a general public installer and hosted binary release are not available. Node is needed only to develop the Review UI; installed core, CLI, and MCP do not require it.

The [user workflow](docs/user-workflow.md), [technical specification](docs/spec.md), and [Agent tool contract](docs/agent-tool-contract.md) describe the current interfaces. `agent-skill/` is the shared source for Skills; `host-integrations/` contains thin host configuration. Codex and other compatible hosts use the same core through CLI or MCP.

## License

Roughcut is open source under the **Apache License 2.0**. You may use, modify, distribute, and commercially use Roughcut under the terms of that license. See [LICENSE](LICENSE).

Third-party software, dependencies, and model assets keep their own licenses and are not automatically covered by Roughcut's Apache-2.0 license. See [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
