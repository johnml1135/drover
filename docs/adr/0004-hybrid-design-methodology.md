---
schema_version: 1
id: ADR-0004
status: accepted
date: "2026-10-20"
decision-makers: [johnm]
applies_to: ["drover/**", "hub/**", ".drover/**", "docs/**"]
enforcement: null
supersedes: []
links:
  - rel: methodology
    target: docs/superpowers/specs/2026-10-20-hybrid-methodology-design.md
---

# Use branch-carried design artifacts with format adapters

## Context and Problem Statement

Drover must coordinate continuing product, architecture, and implementation
work without imposing one document format or confusing approved decisions
with implemented behavior. The owner approved the hybrid direction on
2026-10-20; accepted 2026-10-20: accepted specs live on main, superpowers is the first adapter, and the design was written up as a contract.

## Decision Drivers

Preserve human decision rights, repository conventions, recoverable handoffs,
early verification, and economical orchestration.

## Considered Options

* One mandatory format: simple extraction, costly repository migration.
* Independent framework-specific workflows: native conventions, duplicated gates.
* Shared artifact model with adapters: common gates and preserved authored text.

## Decision Outcome

Choose the shared model: intent, decision, contract, task, and evidence.
Use superpowers first and plain as fallback.

Carry in-flight work on branches. Publish accepted decisions directly to main
through owner actions. Synchronize living behavior specs when implementation
lands. Generate the design index and event stream from Git records.

Check newly changed ADRs after rebase using scope, scripts, then model triage.
Begin in shadow mode. Keep owner questions concise and urgency-aware.

## Consequences

Adapters require versioning and conformance tests. Recovery requires event and
evidence refs as well as branches. Missing metadata prevents readiness.
Acceptance and implementation remain distinct.
