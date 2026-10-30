---
schema_version: 1
id: ADR-0011
status: accepted
date: "2026-10-29"
decision-makers: [johnm]
applies_to: ["drover/**", "hub/**"]
enforcement: null
supersedes: []
links:
  - rel: related
    target: docs/adr/0007-one-git-repository-per-repo.md
---

# Engine-only stage advances and single output acceptance

## Context

A generic status update could assert success without a run, and a worker's existing commit could bypass scope validation. Owner decisions D2 and D3 reserve Drover-run success claims for the engine and require provable, scoped output.

## Decision

The ledger reserves claimed, running, gated, reviewed and landed for engine operations. These operations check their run, gate receipt, review verdict and exact commit evidence alongside the transition. Owner acceptance of read-only findings and external landing remain explicit attestations. Writable work requires a scope before approval; drafts and imports may remain incomplete.

Worker output enters one acceptance path whether it is an existing commit, a request to commit, or an environment-blocked result. Acceptance checks the run's captured scope and base, the exact commit, ancestry and a clean checkout before gates begin. Out-of-scope output is refused. Self-run tasks and the broader unfinished-output checkpoint policy are subsequent work under D2 and D3, respectively.
