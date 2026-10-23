---
schema_version: 1
id: ADR-0009
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
    target: docs/adr/0010-supervised-directors-and-task-joins.md
  - rel: design
    target: docs/superpowers/specs/2026-10-23-durable-subscriptions-design.md
---

# Deliver durable personal summaries and explicit broadcasts

Directors need important updates without reacting to every worker completion.
Keep selective notifications and personal summaries in Drover's ledger, with
job-specific defaults and agent-adjustable detail and frequency. This records
the owner's 2026-10-23 direction; the existing in-memory UI event stream does not
already provide these guarantees.

## Decision

Use the existing durable event ledger with persistent subscriptions, inbox
deliveries, acknowledgements, and summary coverage. State changes and notification
intent must be transactionally consistent or projected through a durable cursor
in the same transaction as inbox insertion. Delivery is at least once, with stable
event/message identities and idempotent resulting actions. A successful transport
write is not acknowledgement that an agent processed an event.

Each director receives only its owned ordinary events under ADR 0008, plus
explicitly addressed broadcasts. Provide quiet, normal, and verbose preferences,
and one update operation for scope, event classes, summary interval, detail, and
wake behavior. Tuning cannot grant authority, change event ownership, or discard
unresolved issues. Human-owner escalation remains visible when muted, unavailable,
or exhausted handlers cannot act.

Default to immediate actionable decisions, unrecoverable issues, and accepted
final results relevant to the director's work. Routine starts, progress, gates,
retries, and small recorded decisions enter that director's summary. Grouped
research normally wakes its director at the accepted join, rather than for each
child. A worker's completion is not necessarily task completion or landing.

Offer a 15-minute summary by default, assembled deterministically from committed
facts and links with no model calls. Skip empty intervals. Recording a summary
and waking a model to read it are separate settings: summaries are available on
the next run, and periodic summary wakeups are opt-in and budgeted. Coalesce
pending wakeups; never run two instances of a director to deliver more updates.

Support two explicit broadcast modes: include a notice once in every addressed
director's next summary, or urgently deliver it to all addressed directors.
Urgent broadcasts bypass normal summary delays and mute preferences, with
independent acknowledgements and visible offline/failed recipients. Snapshot the
target directors; persistent controls also apply to new or replacement sessions
while active. Publishing global controls requires explicit control authority.

`STOP WORK` is a durable hub control that stops admission immediately and requests
supervised pause/stop of active runs, preserving work and reporting uncertain
termination. Enforcement does not wait for a model or an acknowledgement. Resume
is a separate authorized action. `RELOAD THE SKILL` names the required skill
version and holds affected work until that version is loaded and acknowledged,
including by replacement sessions. Free-text notices cannot impersonate these
structured controls, and receipt alone does not prove compliance.

## Consequences

HTTP receive/acknowledge, CLI, durable SSE, session wakeups, optional signed
webhooks, and a possible local pipe share one subscription/delivery interface.
Start with HTTP/CLI and a verified session adapter; an unsupported push or resume
capability must be reported honestly. Avoid a mandatory external broker or
independent adapter-owned queues. Persistence and explicit replay add code, but
keep disconnected agents recoverable and model usage proportional to decisions.
