# Roughcut source and package contract

Git's full source commit identifies a package build. The release builder injects that identity in staging, builds a wheel and source bundle from the same staged source, and records checksums. Tracked `_build_identity.py` remains without an embedded commit to avoid self reference. A package build requires a clean committed tree.

The source bundle contains the Python core, canonical Skills, thin host integrations, bootstrap and release scripts, README, [installation instructions](../installation.md), and [Agent tool contract](../agent-tool-contract.md). Historical trial notes and internal acceptance material are not package inputs. Tests remain in the public repository but are excluded from the installable source bundle. The builder verifies required files, archive safety, and absence of build caches or large media components.

The default code package does not bundle FunASR, Torch, models, Audalign, FFmpeg, runtime bindings, projects, media, or credentials. Installation probes compatible local components first. Missing components follow the catalog's exact identity and checksum rules; a fresh plan and explicit approval precede apply. Reusing an existing healthy component does not rewrite it. The CLI and MCP read the same persistent runtime binding.

Package manifests and reports may contain versions, source commit, component identities, and checksums. They must not contain user paths, media, API keys, signed URLs, or private project data. A general public installer and hosted binary distribution are not currently published.
