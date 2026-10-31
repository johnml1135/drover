---
schema_version: 1
id: ADR-0012
status: accepted
date: "2026-10-24"
decision-makers: [johnm]
applies_to: ["drover/**", "hub/**", "docs/**", "GLOSSARY.md"]
enforcement: null
supersedes: []
links:
  - rel: related
    target: docs/adr/0008-exclusive-director-event-ownership.md
  - rel: related
    target: docs/adr/0009-durable-director-summaries-and-broadcasts.md
  - rel: related
    target: docs/adr/0010-supervised-directors-and-task-joins.md
  - rel: roadmap
    target: docs/superpowers/plans/2026-10-23-durable-subscriptions.md
  - rel: evidence
    target: docs/superpowers/plans/2026-10-23-durable-subscriptions-dogfood.md
---

# Make finite task execution the default

Drover's default is a written plan and handoff followed by finite builders,
gates, and reviews. This keeps work reviewable and bounded; fresh model sessions
are reserved for concrete corrections or decisions, and invalidated plans return
to the planner instead of expanding into open-ended supervision.

## Decision

Plan the work, hand off a durable contract and checkpoint, and let Drover
orchestrate finite builders, gates, dependencies, and reviews. A concrete failed
gate or review may receive one or two fresh correction-agent sessions within
the existing contract and branch. State the count and applicable spend or time
ceiling before dispatch. If evidence invalidates the plan, or the correction budget is exhausted, stop the affected
chain and return a replanning packet containing the failed assumption, current
evidence and results, and questions that need a planning decision. Do not poll
agents for progress or continue by silently broadening the plan. Close with the
measured outcome and its evidence coverage; unavailable or incomparable measures
remain explicitly unknown.

The primary optional director use case is the current interactive planning
session. It may subscribe to execution failures that could invalidate its next plan. It
does not require a separate resident agent or an automatically launched or
replaced model session. Multi-director scale-out, automatic model-based director
replacement, and paid director-executor enablement are parked product work.

Give workers complete plans, acceptance and terminal-result contracts up front.
Drover should handle roughly 90% of orchestration: sequencing, bounded retries,
gates, waits and completion signals. Minimize direct interaction with running
agents; evaluate each terminal result once. A missing or overdue signal permits
one bounded reconciliation. This is an operating target, not a measured result.

Keep durable event and notification records, acknowledgements, checkpoints,
accepted-result joins, stable identities and deduplication, authentication,
authority fences, STOP controls, and owner-visible unresolved issues. These
provide safe, recoverable task execution and remain applicable to optional
director use; they do not imply that a model is kept resident or woken
automatically. ADRs 0008–0010 remain in force for the optional role's exclusive
event ownership and notification safety. This decision supersedes only their
default-priority and activation implications.

## Status boundary

The source contains director supervision/runtime code and durable notification,
checkpoint, join, and authorization mechanisms. That source evidence does not
establish deployment: automatic director execution is disabled, and a supported
production executor plus interactive-session push/resume remain incomplete or
unverified. This ADR records product priority; it does not claim runtime changes
or deployment. The accepted-work measurement proposal on `fix/durable-integration`
has not been reviewed by the owner. Until then, outcome reports use available
evidence and identify missing coverage rather than assuming a measurement system
exists.
