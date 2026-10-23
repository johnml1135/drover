---
schema_version: 1
id: ADR-0010
status: accepted
date: "2026-10-23"
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
    target: docs/superpowers/specs/2026-10-17-task-types-design.md
  - rel: design
    target: docs/superpowers/specs/2026-10-23-durable-subscriptions-design.md
---

# Supervise director roles and join accepted task results

Work should continue when an initiating agent disappears, and parallel research
should wake its director when the required results are collectively ready.
Drover owns that continuation through durable roles, bounded recovery, and
explicit task-group joins. These are accepted architectural decisions from
2026-10-23, not a claim that the runtime mechanisms have landed.

## Decision

Persist a director's scope, authority, brief/checkpoint, decisions, result
references, notifications, and current session reference. Let its model invocation
sleep or end while ordinary code observes progress, releases waits, and prepares
notifications. Dormant directors consume no worker seat or model tokens.

On an actionable wake, use existing dispatcher admission, allowance reserves,
capacity, and run accounting. Resume a verified compatible session when supported;
otherwise admit a replacement invocation from the durable record. A live worker
continues when its director dies. Reconcile existing run evidence before any
restart; unknown process state is not proof of death.

Use one current execution lease per director and one handling lease per issue,
with generations fencing obsolete acknowledgements and actions. The replacement
serves the same director identity and retains its ownership and personal summary.
No second scheduler or persistent model-based monitoring loop is introduced.

Recovery uses a scoped action allowlist, durable attempt and wake/token budgets,
and normal task approval and execution rules. Prefer deterministic handling;
wake a model only when a decision or admitted task requires one. Exhaustion,
refusal, pause, uncertainty, and unsupported resume produce retained reasons and
a bounded fallback to the human Inbox. Recovery cannot widen privileges, skip
gates, or silently create an extra director tier.

Represent parallel-result collection as a task group with explicit membership,
an owning director, and a completion predicate. Seal membership before completion.
The default join requires accepted successful completion of every required child;
failed or cancelled children are not success. Record one group completion and
result manifest transactionally, with stable identities across retries/restarts.

A collection-only parent may complete when its accepted join is satisfied. A
parent that owes synthesis or implementation merely becomes eligible for that
next work; it does not complete automatically. Normal child completions can stay
in the owning director's summary while the join produces one actionable wake.
An exceptional child issue may wake the responsible director sooner. Cross-scope
joins obey ADR 0008's exclusive routing and permitted result sharing.

## Consequences

Prefer durable workflow state over keeping a chat process alive or periodically
asking a model whether work finished. Reuse the ledger's waits, run adoption,
task lifecycle, and action-outbox contracts. Explicit groups and fencing require
more state than parent links and polling, but avoid false completion, duplicate
recovery, lost results, and token-consuming supervision.
