# Durable subscriptions and director recovery

Date: 2026-10-23. Status: draft for owner review.

## Intent and decisions

Drover owns continuing work after the initiating agent disconnects or dies. Agents subscribe to selected work, receive actionable updates, and can take responsibility for a specified subset of stuck tasks. Parallel research can finish independently and wake a director once the required results are ready. Waiting, filtering, delivery, ordinary recovery, and digest construction spend no model tokens.

Owner requirements from this conversation:

- Automatically subscribe an initiator to relevant updates about work it starts.
- Select some tasks and events without subscribing to everything.
- Configure which agent handles a subset of stuck work; replace a dead agent when appropriate.
- Support explicit parent/child completion conditions, including two research children joining before the director is notified.
- Make this part of the harness, with job-specific defaults rather than every completion waking every agent.
- Offer periodic summaries, such as every 15 minutes, including routine progress and small decisions. Agents can easily increase or decrease notification detail and frequency.
- Use engineering judgement to select useful defaults and bounded recovery.
- Support typically two to five independent directors and validate ten. Each ordinary event belongs to exactly one owning director, with a separate summary per director. Deliberate broadcasts may enter all summaries or urgently address all directors.

Recommended defaults in this draft: stable director identities, immediate actionable notifications, quiet routine work, an available 15-minute digest, and bounded automatic recovery within already granted authority. A digest is recorded without waking a model by default; a director may explicitly select periodic digest wakeups with a wake budget. The current chat receives no automatic notification until a compatible session adapter is connected.

The accepted architectural decisions are recorded in ADR 0008 (exclusive director ownership), ADR 0009 (durable personal notifications and broadcasts), and ADR 0010 (supervised directors and joins). This draft supplies implementation detail for review; an accepted ADR does not claim its runtime behavior is already implemented.

## Existing behavior and integration

Inspected baseline: `95771b9` on 2026-10-23.

- `drover/ledger/schema.py` persists append-only task events and task dependencies. `drover/ledger/waiting.py` persists waits, cause identities, deadlines, and replan requests.
- `hub/events.py` provides UI SSE through an in-memory ring of 200 events. Its event IDs and replay history do not survive a hub restart. It is a useful live view, not a reliable agent inbox.
- `drover/dispatcher.py` adopts existing runs, detects missing runners using repeated clean scans, and requeues orphaned work. A failed process scan means unknown, not dead. This recovery remains authoritative for worker execution.
- The 2026-10-17 task-types design specifies durable action and notification outboxes, an owner inbox, authenticated actions, successful-completion dependencies, and resumable sessions. Much of that is a design contract rather than a fully implemented runtime. Extend those contracts; do not create another task engine, ledger, owner inbox, or scheduler.
- `T-b7` covers bounded progress observation and durable refused-recovery notices. Its candidate is not yet landed. Consume its eventual semantic observations without introducing another persistent LLM monitor.
- ADR 0002 keeps dispatch in the Windows hub; ADR 0007 keeps one repository with placement-owned worktrees. A director adapter does not become a second dispatcher.

`parent_id` in the task-types contract is navigational. Existing dependency/wait semantics must be explicitly extended for groups; a parent link alone must never change completion or approval.

## Approach

Use the existing ledger as the durable event source and add selective subscriptions, durable inbox deliveries, and role supervision there. This keeps task changes, event identities, and notification intent consistent and provides local recovery without another deployed service.

Two alternatives were considered. Standalone webhooks are a useful delivery adapter, but alone leave subscription ownership, replay, task joins, and dead-agent recovery unsolved. An external message broker offers additional deployment and operational cost without removing the need for ledger transactions and task authority checks. Neither is the default for this local harness.

The module exposes a small interface: follow/update/unfollow, receive/acknowledge, and claim/release a scoped handling assignment. Submission can atomically create a task and its initial subscription. HTTP, CLI, SSE, webhooks, and session wakeups call that interface. Transport adapters do not interpret task success, grant recovery authority, or maintain independent subscription state.

## Identity, subscription scope, and authority

A subscriber is a durable principal, such as a named director role, a human inbox, or an external integration. A backend session ID, process ID, pane, or socket is a current delivery address, not the subscriber's identity.

A subscription stores:

- Subscriber and subscription IDs, enabled state, revision, creation event position, and expiry/stop condition.
- Selectors: explicit tasks, a task group, descendants, epic, or repository; job types; semantic event classes; and reason codes. Combine scope selectors as a documented union, then intersect type/event/reason filters. Exclusions take precedence. Reject unknown selector keys and event classes.
- Mode: immediate, digest, or muted, with event-class overrides; digest interval, minimum severity, detail limit, and whether a digest wakes the role.
- A separate handling assignment, if granted, with action allowlist, matching scope, priority, recovery budget, fallback, and policy provenance.

On submission, atomically record the initiating principal, assign the initiating director ownership unless explicitly delegated, and create its follow subscription from that same transaction. An immediate completion cannot race subscription creation. Automated callers supply their authenticated role identity; caller-controlled text cannot impersonate another role. Existing owned tasks support following from now or explicitly replaying retained history.

Each ordinary event has at most one owning director; a routable event has exactly one. Task/group ownership supplies the default, inherited by children when they are submitted. Explicit, authorized delegation can assign a bounded event class, such as stuck-task recovery, to another director. The effective routing partition must still yield one director for each event. Reject conflicting subscriptions/delegations instead of selecting an arbitrary winner or waking both. An unowned event is retained as unassigned and sent to the human Inbox for resolution; do not fan it out to directors.

Following does not authorize intervention or establish ownership. A director can subscribe only to its owned event partition and can narrow or mute it. Only one active handling lease owns a particular issue. Human UI visibility and registered non-agent integrations can observe authorized facts without becoming directors or spawning additional handling runs. Agents cannot enlarge read access, recovery action authority, approval, spending limits, or task execution scope by changing notification settings.

Ownership transfer is an explicit, authorized transaction with expected ownership revision. It transfers pending deliveries and issue responsibility, preserves acknowledgement history, and invalidates the previous director's handling authority. Previously delivered summaries remain historical records; unsent summaries exclude transferred pending work. Replacing a dead session of the same director retains ownership and subscriptions; it does not transfer work to another director. Resolving an issue may produce a new scoped result event, but the same ordinary event is never actively addressed to two directors at once.

An initiating role follows the submitted task or group by default. Following a group covers its owned, explicitly enrolled children, including later children enrolled before the group is sealed. A general descendant subscription is separately selectable but is always constrained by ownership. Do not subscribe the role to an entire repository implicitly. Cross-director dependencies exchange permitted result references: a child's original completion belongs to its director, and a distinct dependent/group-ready event belongs to the director whose work becomes eligible. Do not duplicate the child's event into both directors' summaries.

Stop conditions include task/group completion, cancellation, explicit unfollow, role retirement, and configured expiry. Retiring a role with unfinished work requires explicit reassignment or a visible held state; it must not orphan work silently. Retain already created inbox records for audit. Paused work creates no new recovery attempts; pending facts remain available. Expiry or muting never deletes the task's unresolved owner-visible issue.

## Event classes and job-specific defaults

Events describe durable facts: task result accepted, task landed, group ready, decision requested/answered, progress stalled/recovered, recovery refused/exhausted, or role unavailable. Existing ledger kinds remain stable; an explicit versioned mapping supplies semantic classes. Do not interpret worker process exit or worker `done` as accepted task completion.

| Work followed | Immediate by default | Routine treatment |
| --- | --- | --- |
| Standalone research/investigation | Accepted final result; a decision needed; terminal failure | Start, progress, retries, and recorded minor decisions enter the digest |
| Research in a joined group | Group accepted completion; child issue requiring intervention | Individual accepted child results enter the digest; no separate completion wake |
| Implementation/fix | Accepted delivery/landing; a decision needed; recovery exhausted | Worker completion, ordinary gates/reviews, bounded retries, and small decisions enter the digest |
| Gate/review/mechanical child | An issue outside its granted recovery policy | Successful stages enter the digest; enclosing task/group supplies final notification |
| Director coordinating a group | Group ready; assigned actionable issue; required owner answer available | Other relevant changes enter the digest |

Both human and agent consumers can override these defaults. Task type contributes the initial profile; explicit task settings override it; explicit subscriber settings override delivery preferences for that subscriber. Authorization and mandatory retention rules always constrain the result. Pin the resolved profile version for audit, and apply later agent tuning prospectively unless replay is requested.

Unresolved issues stay in the durable inbox even if their wakeups are muted. If the assigned handler is unavailable or its budget is exhausted, route once to its configured fallback, ultimately the owner inbox. Do not broadcast an issue to all agents.

## Digests, tuning, and token use

Each director has its own inbox, summary settings, event positions, and digest coverage. The initial digest interval is 15 minutes. Generate a nonempty digest at most once per interval, from that director's committed subscribed events and explicitly addressed broadcasts. Empty intervals do nothing. Digests can be read whenever an agent next runs; recording or updating a digest causes no model call. One director's summaries, wakeups, acknowledgements, and tuning never advance another's cursor or alter its preferences.

An agent may opt into periodic digest wakeups. Those wakeups use the role's normal admission, per-hour wake limit, and token budget. Coalesce a digest with an already pending immediate wake. Never start another run of a role merely to deliver a second update while its current run is handling the inbox.

Generate summaries deterministically: counts, latest states, relevant decision text, reason codes, actors, affected tasks, and links to results or evidence. Group repeated retries and replaced states. Distinguish proposals, committed decisions, and decisions already handled. Do not invent conclusions or re-summarize full worker transcripts with a model. Bound entry count and bytes; retain a link/cursor for omitted details.

Subscriptions support named presets (`quiet`, `normal`, `verbose`) plus explicit settings. Quiet retains assigned actionable wakeups and stores routine changes for later reading. Normal adds final task/group wakeups and the available 15-minute digest. Verbose exposes stage changes and more detail but still coalesces wakeups. Muting is explicit and distinct from quiet.

Agents can change interval, wake-on-digest, event filters, owned task scope, and detail with a single idempotent update. The response returns the resolved settings, next digest time, and a preview of which recent events would match. Version preference changes against source event positions so projection after restart uses the preferences that applied to each event. Changes do not discard unread messages. Shorter intervals must not replay previously summarized ranges; disabling a digest wakeup leaves existing digest content readable. Revoked read access prevents future offers of protected content even if it was previously queued.

Proposed CLI examples, not commands available in the inspected baseline:

```text
drover follow T-research --as director:feature --preset normal
drover follow G-research --as director:feature --on group-ready,decision-needed
drover notifications tune --as director:feature --digest 15m --digest-wake off
drover notifications tune --as director:feature --preset quiet
drover inbox --as director:feature --wait --json
```

No permanent model-based sheriff or director polling loop is required. Timers and process/progress observation are ordinary code; model calls happen for admitted work or an explicit judgement request. An alive/outputting process is not proof of useful progress, and elapsed silence alone is not authority to kill one.

## Shared notices and urgent control broadcasts

A broadcast is an explicit message class, separate from ordinary event routing. It has an authenticated issuer, stable ID, target set, urgency, bounded body, optional structured control, and expiry or durable supersession rule. Summary broadcasts are included once in each addressed director's next summary without forcing a wake. Urgent broadcasts bypass summary delays and quiet/muted preferences, with one independent delivery and acknowledgement per director. Snapshot the current target directors when publishing; persistent controls also apply when a director registers or restarts while the control remains active.

Ordinary agents may send summary notices only when policy grants that capability. Global controls require explicit control authority, normally the human owner or the hub acting under an approved policy. Notification-tuning permission cannot mint broadcast or global-control authority.

`STOP WORK` is a structured hub control as well as an urgent notice. Persist a stop epoch and stop admission immediately, before waiting for director acknowledgements or invoking any model. Stop/pause active runs through their existing supervised mechanisms, preserve their work, and expose uncertain termination; do not equate message delivery with confirmed cessation. Every director records the current control epoch before taking its next action. Resume requires a separate authorized action; a message acknowledgement cannot resume work.

`RELOAD THE SKILL` identifies the skill and required revision/digest. Invalidate admission for affected actions/runs until the required version has been read and acknowledged by each affected director session. An acknowledgement cites the actual version; generic delivery receipt is insufficient. A restarted/replacement session must load the still-required version. Existing incompatible work pauses at a supervised safe point or is held for an authorized decision; do not silently hot-swap running instructions. Plain free-text urgent notices demand attention but cannot impersonate these structured controls.

Keep per-director received, applied, and failed/held status so the owner can see which of ten directors has complied. Offline directors retain urgent deliveries, and new sessions must observe active controls before receiving ordinary work. Duplicate control delivery is idempotent. A director's normal wake budget cannot prevent enforcement of a hub stop or version fence; those are deterministic checks, not model wakeups.

## Durable delivery and transports

Persist task state and its event in the existing ledger transaction. Also persist matching notification intent there, or consume that event through a durable projection cursor whose inbox insertion and cursor advancement share a transaction. Creating a task's subscription in the task transaction eliminates the first-event race. A unique `(subscriber_id, source_event_id, notification_class)` prevents duplicate inbox entries from replay or overlapping filters owned by the same subscriber. Persist the routing ownership revision; an explicit transfer updates pending delivery authority atomically. Broadcast delivery identity additionally includes its addressed director.

Maintain separate facts for materialized, offered, and acknowledged messages. A successful webhook response or socket write means delivery receipt, not that the director processed the event. Acknowledgement requires message identity and the current consumer lease; action acceptance is separately recorded by the task action interface. Preserve required work as durable task state, not an inference from an acknowledgement.

Delivery is at least once. Every envelope includes stable delivery/event IDs, schema version, subscriber, task/group/cause identities, time, severity, reason, expected revision, and bounded evidence references. Consumers use idempotency keys for resulting actions; a stale message must recheck current task state. Do not promise exactly-once network delivery.

First adapters:

1. Authenticated HTTP receive/long-poll plus explicit acknowledgement and a CLI wrapper. A small waiting client uses no model tokens.
2. An agent-session adapter that offers a coalesced inbox wake to a connected director, or starts an admitted replacement run when permitted. A supported backend must prove session identity before resuming. Current interactive chats are not assumed to have a push or resume capability.
3. UI SSE fed from durable identities and replay. Preserve current UI compatibility while replacing its role as an authoritative replay source.

Signed webhooks are an optional subsequent adapter with endpoint registration, bounded request timeout, retry/backoff, secret redaction, and delivery audit. A named pipe can be added for a Windows-local consumer if needed. HTTP is the initial portable path between Windows, WSL, and remote agents; no caller must depend on a Windows pipe to use subscriptions.

Slow consumers have bounded batches and payload sizes. Retry delivery with capped exponential backoff; after the configured limit retain an actionable failed-delivery entry and use the configured fallback. One bad endpoint must not block dispatch or other subscribers. On history retention, retain unacknowledged entries and all unresolved issue facts; an unavailable replay range returns an explicit resync requirement plus current scoped snapshot, never silent success.

## Scoped handling and agent recovery

A director role stores its purpose, owned event partition, approved capabilities, subscription settings, model/seat policy, durable brief, decision/result references, current backend session reference, and generation. Store control state and small checkpoints, not continuous transcript copies. Checkpoints are versioned artifacts; inbox acknowledgements and resulting action receipts refer to the committed checkpoint where needed. Support and test ten independent roles, typically two to five in use, without requiring another coordinating director or a new hierarchy. All share normal capacity accounting; dormant roles consume no worker seat.

Ordinary dispatcher recovery continues to adopt a living worker and reconcile terminal run evidence. Director recovery does not restart child workers merely because their director disappeared. Unknown process/session state is not proof of death.

Handling an issue acquires a time-bounded lease on `(role, issue identity)`; executing a role also requires a single active role lease. A generation/lease token fences old agents from acknowledging or acting after replacement. All action requests validate both current lease authority and expected task revision. Multiple observers never imply multiple recovery actors.

When a role is needed but its consumer is confirmed gone or its lease has expired, inspect existing run/session evidence first. Resume the verified compatible backend session if available. Otherwise launch a fresh run from the durable brief, current scoped state, accepted results, recorded decisions, checkpoint, and unread inbox. Do not duplicate a possibly active run when evidence is uncertain; record the uncertainty and escalate through the fallback policy.

The recommended initial recovery profile allows one replacement of a dead director and up to two task retry/restart actions per issue. These are upper bounds, subject to task approval, existing dispatcher retry limits, resource admission, allowance reserve, and configured token/wake budgets. The lower applicable limit wins. Count attempts durably across hub and role restarts; reset only after confirmed resolution or an explicit authorized policy action. Transport retries do not spend task retries.

A role may run only its preauthorized action allowlist. Recovery cannot approve new work, increase model/seat privilege, widen a sandbox, bypass a gate/review, or terminate an uncertain process. Pause, cancellation, exhausted budget, unsupported resume, and denied actions become explicit durable reasons. Only a new episode or changed evidence creates a new wakeup; repeated observation of the same issue updates its existing inbox record.

## Task groups and joins

Represent a join as an explicit task-group record linked to an optional parent task, with a revision, enrolled required children, membership state, success predicate, and failure policy. Membership is open while children are being registered and then explicitly sealed. Never complete an empty or still-open group by accident. Enrolling children and sealing use expected revisions and idempotent request keys.

Default `all-success`: once sealed, every required child must have accepted successful completion according to its pinned task lifecycle. Failed/cancelled children do not count as success. Retryable child failures keep the group waiting; an issue outside recovery policy creates one actionable group issue. The default terminal failure policy blocks the group pending a handler or owner decision. Partial acceptance or replacement children require an explicit permitted action and audit event.

In one ledger transaction, record the final child completion evidence, evaluate the group, and transition a satisfied group once with an immutable result manifest. A unique completion identity prevents repeated group-ready deliveries after restart. The manifest names child task/revision/result hashes or landed commits. Group members' normal completion events remain queryable even when no individual wake is selected.

For a parent whose only job is collecting results, accepted group completion may complete that parent under its declared lifecycle. For a parent that must synthesize findings or implement a solution, the join makes its next stage eligible; that parent is not complete until its own acceptance criteria pass. The director receives the group-ready event once, then does synthesis only if the workflow calls for it. Failure and urgent child issues may wake it sooner.

Example: a director starts a research group with two children and seals it. Child A completes; its result enters the manifest and digest without waking the director. Child B completes; the accepted all-success join records one group-ready delivery. Drover wakes the director once with both result references. If the director died during research, the same durable role receives that delivery through a verified resumed session or an admitted replacement.

## Persistence and execution boundaries

Extend the ledger with versioned records for exclusive routing ownership/delegations, subscriptions/preferences, personal inbox entries/digest coverage, delivery attempts, broadcasts/control epochs and per-director application status, roles/checkpoints/leases, scoped handling assignments, and task groups/membership/completion evidence. Reuse the task-types action outbox and owner notification contracts when they land; if absent, implement their required shared subset once and document the dependency. No private notification database or adapter-owned authoritative cursor.

Resolve event matching and digest construction in ordinary hub code. Keep delivery I/O outside ledger transactions. Launch director runs through existing dispatcher admission and run accounting, using idempotent durable launch intent and the existing nonce/adoption rules. Never call a model directly from a webhook handler, SSE generator, or database transaction.

Backend session capabilities vary. The adapter must state whether it supports push to a living session, verified resume, or only a fresh run. An unavailable capability yields a visible held/unsupported result or the configured admitted fallback. It must not report a wakeup as successful merely because it wrote text into a file or a dead terminal.

## Error handling, observability, and validation

Expose pending inbox count, oldest unhandled actionable age, delivery failures, role generation/lease state, suppressed/coalesced wakeups, recovery attempts/refusals, digest coverage, and group membership/result state through existing health/task views and ledger queries. Log policy version and cause links; redact credentials. Distinguish a healthy hub heartbeat from task progress and consumer liveness.

Required acceptance scenarios use fake time and controlled execution/delivery adapters:

1. A task finishes immediately after submission; its initiating role receives the event even if it was offline, including after hub restart.
2. Scope/type/event exclusions select the requested subset; changing verbosity cannot increase task authority or read access.
3. Default research grouping produces one final wake; implementation stage completions stay quiet while its accepted landing is immediate.
4. Fifteen minutes of routine changes produces one bounded digest with decision provenance and zero model calls; empty intervals produce none. Digest wakeups are separately enabled and budgeted.
5. Duplicate events, crashes before/after offer, and repeated acknowledgements neither lose work nor execute a resulting task action twice. A webhook receipt alone does not acknowledge processing.
6. Two handlers race for an issue; only the current lease holder acts. An old generation cannot acknowledge or act after replacement.
7. A director dies while its children run; they continue. One admitted replacement restores the role's scoped state and unread messages without relaunching the children.
8. Unknown process state, pause, cancellation, unavailable seats, exhausted allowance, and repeated failed recovery produce precise retained reasons without unbounded wakes or retries.
9. Concurrent final children and hub restart yield one group completion. An open group, cancelled child, or unmet acceptance criterion never produces success. Synthesis parents remain incomplete until their own work passes.
10. A slow or failing subscriber cannot grow unbounded queues, block dispatch, or lose its unresolved issues. Replay outside retention explicitly resynchronizes.
11. Ten directors follow disjoint work concurrently; every ordinary event reaches its owning director only. Conflicting ownership is rejected. Each personal digest has independent preferences and coverage; idling one director does not affect others.
12. Transfer pending work while an old delivery is in flight: only the new owner can act/acknowledge afterward, with no second processing of already accepted work. Cross-director dependency readiness produces a distinct event for the dependent's director, without sharing unrelated child details.
13. One summary broadcast appears once in each addressed personal digest. An urgent broadcast reaches every addressed director despite quiet settings, with independent acknowledgements and no permanent subscription to everyone else's work.
14. STOP WORK halts admission without any model call or director acknowledgement, reports uncertain active termination, survives restart, and fences a newly registered director. Skill reload fences affected actions until the requested version is acknowledged, including by replacement sessions. Free-text notices cannot forge either control.

Integration verification must include a real Windows hub to WSL worker, a director adapter disconnect/replacement, and hub restart between task completion and consumer acknowledgement. Passing fake delivery tests alone does not establish an interactive Codex session can receive push notifications.

## Delivery sequence

Implement in this order so each increment has an observable purpose:

1. Durable subscriptions/inbox, automatic follow, job-specific profiles, HTTP/CLI receive/acknowledge, tuning, and deterministic digests. Keep director wake execution disabled until the next increment is verified.
2. Director role/session adapter, scoped handling leases, bounded replacement/recovery, coalesced admitted wakes, and health visibility.
3. Explicit groups and accepted-result joins wired into those same subscriptions and role wakeups.

Webhooks are an optional adapter after the core is reliable; pipes are deferred until a concrete consumer needs one. Existing task-type work and T-b7 must be reconciled before assigning implementation so parallel workers do not independently create competing outboxes, recovery loops, or event taxonomies.
