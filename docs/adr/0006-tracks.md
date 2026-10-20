---
schema_version: 1
id: ADR-0006
status: accepted
date: "2026-10-20"
decision-makers: [johnm]
applies_to: ["drover/**", "hub/**", ".drover/**", "docs/**", "GLOSSARY.md"]
enforcement: null
supersedes: []
links:
  - rel: methodology
    target: docs/adr/0004-hybrid-design-methodology.md
  - rel: configuration
    target: docs/adr/0005-repo-config-under-dot-drover.md
  - rel: research
    target: docs/research/2026-10-20-configurable-workflow-levels.md
  - rel: research
    target: docs/research/2026-10-20-workflow-levels-design.md
  - rel: research
    target: docs/research/2026-10-20-design-implementation-interface.md
  - rel: research
    target: docs/research/2026-10-20-script-gates-and-model-cascades.md
  - rel: research
    target: docs/research/2026-10-20-process-self-audit.md
  - rel: research
    target: https://www.aihero.dev/skills-retro
---

# Tracks: configurable workflows selected per task

## Context and Problem Statement

The workflow research proposed six ordinal levels, with L5 contract built first.
Different kinds of work need different workflows, but security or GUI work does
not fit a single cheap-to-expensive ordering. We need configurable workflows that
preserve owner authority, explain each selection, and improve from recorded
outcomes. This records the owner's 2026-10-20 decisions and replaces the research
design's ordinal workflow levels; model escalation levels remain a separate concept.

## Decision Drivers

Preserve owner decision rights, repository control, bounded implementation,
independent verification, reproducible selection, and economical orchestration.
Retain ADR 0004's shared artifact model and ADR 0005's configuration scopes.

## Considered Options

* **One mandatory contract workflow:** provides a measurable baseline, but spends
  the same orchestration effort on documentation and architectural changes.
* **Ordinal workflow levels:** offer simple minimum-level rules, but imply every
  workflow can be ranked on one axis and make specialized workflows awkward.
* **Freely composed workflows per task:** flexible, but runtime merging makes the
  effective obligations difficult to explain and reproduce.
* **Named, configurable tracks:** support distinct workflows with explicit
  selection and reusable definitions. Choose this with controlled extension and
  owner-approved changes.

## Decision Outcome

A **track** is a named workflow configuration, selected per task, not an ordinal
level. Supply six built-ins:

* **docs:** documentation with settled intent.
* **mechanical:** repeatable transformations preserving behavior.
* **fix:** a narrow, reproducible defect within an existing module.
* **feature:** a contained feature within existing modules.
* **contract:** explicit planning and independently checked implementation for
  significant refactors, risk, deletion, or uncertainty; build this baseline first.
* **major:** research, architecture, and decomposition into dependent tasks.

Repos override or extend definitions in `.drover/workflows/<track>.yaml`, referenced
through `.drover/config.toml` as required by ADR 0005. Tracks may extend other tracks;
resolve extension before execution into one effective definition. Each task uses
one track, with configuration priority breaking selection ties; do not merge
multiple selected tracks at runtime. A newly needed track requires an
owner-approved ADR on main before use.

Select by configured rules over paths, labels, and size; if unresolved, ask a small
model; if still unresolved, use the configured default. The director or owner may
override the selection. Log every pick, its source and reason, overrides, and the
definition/policy revision. Until validated and owner-enabled, cheaper tracks run
as **contract**; record both selected and applied tracks. Tuning cannot activate
them automatically.

Four owner escalations apply on every track: **user-facing behavior, data models,
main APIs, and new modules**. Repos may add escalations, never remove these. An
existing approval satisfies a checkpoint only while its decision scope is
unchanged. Track selection or override cannot waive owner authority.

The planner-to-implementer agreement is the **build contract**: prose plus a
sidecar describing bounded work and its verification. Its units are **steps**;
all steps merge as the task's one squash. Split oversized build contracts into
several tasks. Planner-to-implementer validation uses **contract checks**; rename
`scope_gate` to `scope_check` when moved into Drover. Reserve **session handoff**
for continuity between model sessions.

Run a **tuning pass** after each merged build contract and on schedule. Following
[skills retro](https://www.aihero.dev/skills-retro), propose ranked improvements
traced to specific runs and logged outcomes. Route each proposal to a script
check, track YAML, brief, standards rule, or research note. Every large-model
finding asks: **could a script or small model have caught this?** The owner
approves changes before a new policy version is published.

## Consequences

Track names replace L1–L6 in policy and vocabulary; historical research retains its
original terminology. Track inheritance and priority need validation and
explainable selection records. The contract baseline supplies evidence before
cheaper workflows are enabled; research supports the choices but does not itself
activate policy. Publish this ADR and the glossary changes on main after Phase 3b.
