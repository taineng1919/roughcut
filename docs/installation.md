# Installation and Update

Roughcut requires Python 3.11 or newer. Give a local Agent with terminal access the repository URL and this instruction:

> Install or update Roughcut from https://github.com/taineng1919/roughcut. Follow `docs/installation.md`. First inspect both the source checkout and the installed runtime, including Core and media component health. Choose Fresh, Update, or Reuse from the observed state. Do not overwrite local Git changes, replace an ambiguous installation, or delete an existing installation. Show me the current media component plan, including exact changes and costs, and wait for my approval of that plan before downloading or installing media components. Ask separately before making system-level changes.

## Mental model

- **Source checkout:** the Git repository used to run `scripts/bootstrap.py`. Its presence does not prove that Roughcut is installed.
- **Installed Core:** the selected `--install-dir` (default: `~/.roughcut`), containing Roughcut's managed `venv/`, CLI/MCP launchers, and runtime binding. Its presence does not prove that a valid source checkout remains. Keep using the same install directory on later runs.
- **Media components:** separately managed runtimes, models, cache, and compatible external tools. A Core update does not update or download these.
- **Host integration:** canonical Skills in `agent-skill/` and host-specific CLI/MCP discovery. A healthy CLI does not prove that a host has refreshed its Skills or MCP registration.

The Agent must identify the user's intended source and install locations before changing either. If multiple plausible install directories exist, ask which one is authoritative. Preserve the chosen managed component root and cache path across updates; this repository defines no canonical defaults for those paths.

## Preflight

1. Locate a real Python 3.11+ interpreter, check its version, and use that same interpreter for bootstrap. For example, `python3 --version`, `python3.11 --version`, or `py -3.11 --version` may work on a particular machine; none is universal. In commands below, replace `<python>` with the verified interpreter. Do not change system Python, global pip, shell startup files, or `PATH` to make a launcher available.
2. Locate the intended source checkout and install directory independently. Check whether `--install-dir` exists, whether it has `venv/` and the platform-specific `roughcut` and `roughcut-mcp` launchers (`venv/bin/` on macOS/Linux, `venv/Scripts/` on Windows), and read `roughcut health --json` from that install's launcher if runnable. Read `roughcut diagnostics --json` where available. An absent directory, a healthy matching Core, an older/different identity, an unhealthy/incomplete Core, and an unknown or ambiguous install are distinct states. Files alone are not proof of health. For an unexplained or conflicting identity, stop and ask which installation is intended.
3. For an existing source checkout, inspect before updating:

   ```sh
   git remote -v
   git status --porcelain
   git branch --show-current
   git rev-parse HEAD
   ```

   Require a clean worktree, attached `main` branch, and official origin before contacting the remote. Then inspect its current `main`:

   ```sh
   git fetch origin
   git rev-parse origin/main
   git merge-base --is-ancestor HEAD origin/main
   ```

   Run these in that checkout. Accept `https://github.com/taineng1919/roughcut` (with optional `.git`) or `git@github.com:taineng1919/roughcut.git` as the official origin. An ancestry exit 0 means local HEAD can fast-forward or is current. If it is nonzero, check `git merge-base --is-ancestor origin/main HEAD`: exit 0 with different commits means local main is ahead; otherwise it diverged. Stop in either case. If fetch fails, stop without guessing remote state. Do not stash, reset, switch branches, or overwrite work.
4. Inspect existing managed root, component cache, any explicit external paths, and current component health or diagnostics. Missing Python or required system dependencies must be reported with the proposed installation method and whether it changes the system; obtain approval before using `sudo`, Homebrew, winget, or another system installer. Core bootstrap and approved Roughcut component apply are separate from those system changes.

## State decision table

| Source state | Installed Core state | Action |
| --- | --- | --- |
| Missing | Missing | Clone official source into a user-selected or new conflict-free directory; run Core bootstrap (**Fresh**). |
| Clean official `main`, current | Missing | Run Core bootstrap (**Fresh**); keep the existing checkout. |
| Missing | Existing, identified | Obtain a clean official source; retain the install directory, then let bootstrap decide **Update** or **Reuse**. |
| Clean official `main`, behind `origin/main` | Existing, identified | Fast-forward source, then run bootstrap; its result determines **Update** or **Reuse**. |
| Clean official `main`, current | Older or different known Roughcut identity | Run bootstrap against the existing install (**Update** if it reports `updated`). |
| Clean official `main`, current | Healthy matching Core | Run bootstrap; expect `reused` (**Reuse**). |
| Clean official `main`, current or behind | Unhealthy or incomplete Core | Record health and identity; if this is the identified Roughcut install, bootstrap may repair it as `updated`. Verify again; stop on failure. |
| Dirty, wrong origin, other branch, detached, ahead, or diverged | Any | **Stop.** Report the Git facts; do not change the checkout or installation. |
| Any | Unknown, ambiguous, or conflicting install identity | **Stop.** Resolve the authoritative install/source identity with the user first. |

Source state and installed Core state are separate dimensions. Do not infer Fresh or Update from a version string, directory name, or checkout alone. The bootstrap's `core_action` is the authority for the final Core result: `installed`, `updated`, or `reused`.

## Fresh installation

If source is missing, clone the public repository into the user's chosen source location or a new location with no existing content:

```sh
git clone https://github.com/taineng1919/roughcut.git <new-source-dir>
```

Inspect the new checkout and selected install directory as in Preflight. From the verified source root, run the Core-only entry point with the verified interpreter:

```sh
<python> scripts/bootstrap.py --install-dir <roughcut-install-dir> --json
```

For a missing install, expect `core_action: "installed"`. This creates the managed Core environment; it does not apply media components. Verify the installed Core as below before any component work. If a selected install already exists, retain it: bootstrap will choose `updated` or `reused` from its own identity and health checks.

## Existing installation update

After the Git preflight passes, a checkout behind `origin/main` can be advanced with:

```sh
git merge --ff-only origin/main
```

Do not use an implicit `git pull`, merge commit, rebase, automatic stash, reset, or forced checkout. If the fast-forward fails or the source identity is in doubt, stop. Run the same Core bootstrap command shown under Fresh, using the *existing* install directory. Bootstrap checks the installed Core identity and health, reuses a matching healthy environment, or updates the existing environment and checks post-update health. It does not delete the install directory or reinstall media components. A failed pip update is not guaranteed to roll back the old Core; follow Safe stop / recovery.

## Reuse / already current

When source is current and the installed Core appears healthy, run the same Core bootstrap command and confirm `core_action: "reused"`. Bootstrap checks health and skips pip for a matching Core. Then inspect component health and host integration; do not reapply compatible components or rewrite already-current host configuration.

## Media component plan and apply

Core and media components have independent lifecycles. After Core is verified, select explicit, stable paths for `--managed-root` and `--component-cache` (reuse the original paths on updates). From the source root, obtain a **full, read-only plan**:

```sh
<python> scripts/bootstrap.py --install-dir <roughcut-install-dir> --managed-root <managed-component-root> --component-cache <component-cache-dir> --verify-components --json
```

The plan reports reusable and missing components, source and license details, checksums, estimated downloads and disk use, FFmpeg requirements, `media_components.plan_hash`, and, when supported and unblocked, `installation_operation.operation_id` and `approved_plan_hash`. Preserve any other selection arguments used for this plan (for example, explicit external component paths, FFmpeg commands, or optional Audalign selection). Existing compatible FunASR, models, Audalign, and FFmpeg should be reused; do not download them solely because Core changed. If all required components are reusable, no apply is needed. If only one group needs a change, propose only what the plan requires.

Show the user the exact current plan, costs, external/system actions, and expected changes. Apply only after they approve that exact plan, using its returned hash and operation ID and the same selection arguments:

```sh
<python> scripts/bootstrap.py --install-dir <roughcut-install-dir> --managed-root <managed-component-root> --component-cache <component-cache-dir> --verify-components --apply-components --approved-plan-hash <approved-plan-hash> --operation-id <operation-id> --json
```

The full plan is bound to the chosen paths, catalog, and selection. A stale plan or changed selection requires a new plan and approval. Do not bypass the plan with a bundle or cache-population command. Keep the returned operation ID. If apply is interrupted or its response is lost, query the retained ID before taking any other action:

```sh
<python> scripts/bootstrap.py --install-dir <roughcut-install-dir> --operation-status <operation-id> --json
```

Check component health after apply and on reuse, with the same component paths and selection:

```sh
<python> scripts/bootstrap.py --install-dir <roughcut-install-dir> --managed-root <managed-component-root> --component-cache <component-cache-dir> --component-health --json
```

Use `--verify-components` with health when a full model checksum audit is needed. FFmpeg and ffprobe must form a compatible pair. Optional speaker recognition requires its separate CAM++ model; multicamera alignment has a narrower supported platform scope. A plan's system-level user action is a separate approval gate, not permission for the Agent to install system packages.

## Host integration refresh

For first use, install/register the integration supported by the chosen host. After a source or Core update, compare the installed canonical Skills and host package with this checkout; refresh only when they differ. On Reuse, leave matching host configuration alone. Codex and WorkBuddy have generated package sources. For example, build a new Codex package from this checkout with the installed MCP launcher (choose a new output directory; use `venv/Scripts/roughcut-mcp.exe` on Windows):

```sh
<python> scripts/build_host_package.py --host codex --output <new-host-package-dir> --mcp-command <absolute-path-to-roughcut-mcp>
```

`--host workbuddy` is also supported. Building a package does **not** install it into a host. Follow the host's actual Skill/MCP registration, trust, and reload procedure, then verify native health/diagnostics against this installed Core. For Claude Code, Pi, and other terminal-capable agents, use the verified CLI or their supported local MCP integration; no unified automatic host installer is provided. See [host compatibility](agent-host-compatibility.md), the [WorkBuddy integration notes](../host-integrations/workbuddy/README.md), and the [tool contract](agent-tool-contract.md). Do not place cloud credentials in host configuration.

## Verification

After each Core install/update/reuse, run the installed launcher (use `venv/Scripts/roughcut.exe` on Windows instead of the path below):

```sh
<roughcut-install-dir>/venv/bin/roughcut health --json
<roughcut-install-dir>/venv/bin/roughcut diagnostics --json
```

Require successful Core health with the expected identity, not just bootstrap exit 0 or launcher files. Review diagnostics and component health before using transcription or rendering. After an approved component apply, require successful operation status where applicable, then rerun component health and installed CLI diagnostics. If host integration was refreshed, verify the host's actual MCP health and Skill discovery after reload. Report the final Core `core_action` and media component state separately.

Local installation requires no Qwen or other cloud credential. Qwen transcription is optional and belongs to a later, explicit private user configuration flow. Do not request an API key during Fresh install, put one in the repository or shell history, or treat credential readiness as a Core health gate.

## Safe stop / recovery

- **Git conflict:** dirty, non-main, detached, ahead/diverged, wrong origin, or failed fetch means stop and report the observed remote, branch, status, and commits. Let the user resolve it; do not auto-stash, reset, or overwrite.
- **Core bootstrap failure:** retain the install directory, failed output, and diagnostic facts. Do not continue to component apply. If the installed launcher still runs, read its `health --json` and report the actual current state. Do not delete the install directory for automatic recovery or promise rollback.
- **Component apply interruption:** use `--operation-status` with the retained operation ID. Do not start a second installation by default. If the approved plan is stale, obtain a new plan and approval.
- **Health failure:** stop and report it. Directory or file presence is not a successful installation.

## Developer/source workflow

Editable installation is for source development, not the ordinary user installation path:

```sh
<python> -m pip install -e "core[dev]"
roughcut health --json
roughcut diagnostics --json
```

The installed Core, CLI, and MCP do not need Node; Node is for Review UI development. There is no general hosted binary release or public installer at present. Source bundle and release details are in the [package contract](release/PRODUCT_PACKAGING.md).
