---
schema_version: 1
id: ADR-0008
status: accepted
date: "2026-10-23"
decision-makers: [johnm]
applies_to: ["drover/**", "hub/**", "docs/**", "GLOSSARY.md"]
enforcement: null
supersedes: []
links:
  - rel: related
    target: docs/adr/0001-windows-hub-owns-ledger.md
  - rel: related
    target: docs/adr/0009-durable-director-summaries-and-broadcasts.md
  - rel: related
    target: docs/adr/0010-supervised-directors-and-task-joins.md
  - rel: design
    target: docs/superpowers/specs/2026-10-23-durable-subscriptions-design.md
---

# Assign each ordinary event to one owning director

Drover supports several independent directors, normally two to five and initially
validated with ten. Ordinary event delivery is exclusive to the owning director,
so orthogonal work does not create duplicate decisions, summary noise, or model
wakeups. This records the owner's explicit 2026-10-23 direction; runtime support
still requires implementation.

## Decision

A director is a durable named role with a defined scope of responsibility. Its
identity survives sessions, judgements, and process replacement. This amends the
glossary's previous definition of a director as a fresh model invocation; model
invocations remain bounded and are released while the role waits.

Assign task/group ownership and an initial follow subscription atomically when a
director starts work. Children inherit ownership unless explicitly delegated.
Subscription filters select event classes within owned work; they do not claim
someone else's work. A recovery specialist may own a delegated class of events,
but the resulting event partition still has exactly one owning director per
routable ordinary event. Reject conflicting ownership/subscriptions rather than
broadcasting or choosing an arbitrary winner. Retain unassigned work visibly for
the human owner to resolve.

Each director has its own inbox, summary, preferences, and progress through its
notifications. Changing one director's settings does not affect the others. The
human owner can inspect all authorized work without becoming another director
subscriber. General UI visibility does not create extra model wakeups.

Transfer ownership explicitly and atomically with the expected ownership
revision. Transfer pending deliveries and handling responsibility, preserve
processed history, and revoke the old director's action authority. There is never
more than one current director recipient for an ordinary event. Restarting a
session of the same director preserves its ownership; it does not redistribute
work. Retiring a director requires reassignment or an explicit held state for
unfinished work.

Cross-director dependencies exchange accepted result references and create a new
event for the director whose work becomes eligible. They do not subscribe that
director to the provider's progress or duplicate the provider's completion event.
Explicit broadcasts are the exception described in ADR 0009.

## Consequences

Prefer independent ownership over general multi-agent pub/sub. This limits
opportunistic observation by other directors, but avoids churn and ambiguous
responsibility. Coordination requires a result dependency, explicit delegation,
ownership transfer, or intentional broadcast. No additional coordinating
director or hierarchy is required merely because several directors coexist.
