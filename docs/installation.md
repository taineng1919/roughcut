# Roughcut installation and media components

Roughcut requires Python 3.11 or newer. A source checkout can install the Python core with `python3.11 -m pip install -e "core[dev]"`, then run `roughcut health --json` and `roughcut diagnostics --json`. The core and CLI/MCP do not require Node; Node is only needed for Review UI development. There is no general public installer or hosted binary release at present.

Use `scripts/bootstrap.py` from a checkout or verified source bundle. It installs or updates Roughcut code in its own environment and reports JSON. A verified `--core-wheel` can be supplied for an offline code update. A code update does not install or change media components by itself.

Before transcription or rendering, inspect the component plan:

```sh
python3.11 scripts/bootstrap.py --install-dir <roughcut-install-dir> --verify-components --json
```

The plan reports reusable or missing components, catalog identities, checksums, license and source information, download and disk estimates, FFmpeg compatibility, and a plan hash. It is read only. Use `--managed-root`, `--component-cache`, or explicit external component paths when appropriate. Existing compatible FunASR, models, Audalign, and FFmpeg can be reused without modifying them. Optional speaker recognition requires the separate CAM++ model. Multicamera alignment requires Audalign and the supported platform profile.

Only after reviewing a current full plan and approving its exact costs and mutations should an Agent use `--apply-components` with the plan's `--approved-plan-hash` and `--operation-id`, preserving the same component arguments. A changed catalog, cache, target, or component selection requires a new plan. The bootstrap records installation operations and can report their status. It never treats a temporary environment variable or Host config as the component authority; CLI and MCP use Roughcut's persistent runtime binding and managed manifest.

FFmpeg and ffprobe must be a compatible pair with required H.264/AAC and filter capabilities. Component health and full diagnostics should be checked after apply. Cloud Qwen credentials are optional and owned by Roughcut's user level private configuration; local health and installation do not require them. Cloud uploads occur only for explicitly marked sources after the user confirms scope, transfer, and cost.

Host packages are generated from canonical Skills and thin host integration templates. Install or refresh the package for the chosen host, then trust/reload its MCP registration and verify native health. Host configuration does not copy project rules or hold cloud credentials. See [host compatibility](agent-host-compatibility.md) and [tool contract](agent-tool-contract.md).

The source bundle and release builder are described in [package contract](release/PRODUCT_PACKAGING.md). Packages must be built from clean committed source, verify checksums, and exclude private media, credentials, and local paths.
