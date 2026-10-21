---
schema_version: 1
id: ADR-0005
status: accepted
date: "2026-10-20"
decision-makers: [johnm]
applies_to: ["drover/repos.py", "drover/config.py", "drover/wizard.py", ".drover/**"]
enforcement: null
supersedes: []
links:
  - rel: research
    target: docs/research/2026-10-20-configurable-workflow-levels.md
---

# Repo configuration lives under .drover/; machine configuration lives in the state dir

## Context and Problem Statement

A setting could live in up to four places (the repo's drover.toml, the machine's
config.toml and repos/<name>.toml, ledger settings, and an app overlay at
<checkout>/.drover/config.toml). Repo registrations could override a repo's scripts and
weaken its checks, and `.drover/` meant different things in different repos.

## Decision Outcome

Two scopes, one entry file each:

* **Repo scope** (versioned in the target repo, read at the commit being worked):
  everything under `.drover/`. The entry file is `.drover/config.toml` (TOML,
  `schema_version = 1`) and is the only entry point; other files are read only if it
  names them. Workflows are YAML (`.drover/workflows/*.yaml`); scripts are
  `.drover/scripts/*.ps1`.
* **Machine scope** (state dir): `config.toml` (seats, models, paths, hub),
  `repos/<name>.toml` with machine facts only (path, remote, tool paths, env), and the
  tuner's `policy.json`. A repo registration may not set repo keys once the repo has its
  `.drover/config.toml`.
* Drover's own app overlay is deleted; Drover's repo is configured like any other.
* Ledger settings are runtime switches (pause, slots, tokens), never configuration.

## Consequences

Phase 3b moves Drover's own configuration and scripts to `.drover/` and adds a
`config explain` command that names the layer each value came from. Other repos keep
the registration fallback until they commit `.drover/config.toml`.
