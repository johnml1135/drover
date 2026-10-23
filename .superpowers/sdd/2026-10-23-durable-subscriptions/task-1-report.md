# Task 1 implementation report

Base: `b2f6d76`. Scope: durable roles, exclusive task/event-class ownership, subscriptions, transactional event routing intent, bounded projection/inbox delivery, deterministic personal digests, retention coverage. No model, transport, process, network, live-ledger, or external delivery effects were used. Session authentication, acknowledgements, processing checkpoints/outbox and authority leases remain Task 2; groups remain Task 3; HTTP/CLI remain Task 4.

## Public interfaces

`from drover.notifications import Notifications, Conflict` is the trusted in-process seam. `Notifications(conn, now=callable)` accepts injectable epoch time. The caller owns the normal ledger connection; all writes join its transaction or use `BEGIN IMMEDIATE` through the existing `tx` helper.

- `register(role_id, **policy)` returns durable role state, generation 1 and revision 1. Existing role registration preserves its identity and policy. Role notification defaults may be supplied as `notifications={...}`. Queue limits are `max_pending_routine` (default 1000) and `max_pending_actionable` (default 10000).
- `assign(task_id, role_id, expected_revision=None, event_class='*')` creates ownership and automatic task follow. Existing transfers require the current partition revision; repeated assignment to the current owner is idempotent. A named event class overrides the default `*` owner for exactly that class. Ownership changes append internal `notification.ownership-assigned` audit events.
- `follow(role_id, selectors, **preferences)` returns a stable subscription, revision, resolved preferences, next digest time, and bounded preview. Identical follow requests reuse the subscription. Explicit `replay_after=<event position>` permits a bounded historical replay; responses contain `replay_through` and `replay_more`. A compacted unavailable delivery range returns `resync_required` and a current owned snapshot.
- `tune(subscription_id, expected_revision, **preferences)` appends a version at the current event position. Identical retries return the current version. Stale conflicting revisions raise `Conflict`. `selectors={...}` changes scope; `enabled=False` unfollows prospectively without removing unread deliveries.
- `project(limit=100)` reads source events using `id > cursor ORDER BY id LIMIT ?`, materializes intents, and advances the durable projection position in the same transaction. Limit is 1–1000. Replay of already projected intents does not mutate processed history.
- `receive(role_id, limit=50)` returns at most 100 stable envelopes. Immediate entries and retained actionable issues are offered, followed by generated personal digests. Offers recheck effective ownership. Offering updates `offered_at` and never acknowledges processing.
- `digest(role_id)` creates and returns one nonempty deterministic summary when that role's interval is due, otherwise returns `None`. Default interval is 900 seconds, digest wake is false, detail is 50 entries, and bodies are capped at 32768 encoded bytes. Generated digests remain readable through `receive`.
- `health(role_id=None)` exposes role state, subscriptions/previews, queue counts, oldest unresolved actionable age, acknowledgement count, digest/compaction coverage, storage admission hold, global unassigned count and projection position.
- `check_admission(role_id)` and `scoped_snapshot(role_id)` are additional small public helpers for later adapters. `core.effective_owner`, `core.classify`, and source capture/teardown hooks are shared internal helpers.

`ops.create_task(..., initiating_role=None, group_id=None)` records ownership/follow in its existing transaction before the first source event. A failed enrollment rolls back the task and events. A non-null group ID explicitly raises until Task 3 provides real enrollment. `ops.events(..., after=0, limit=None)` applies source positions and SQL limits while preserving its existing default behavior.

## Selector and profile rules

Supported scope selectors are `tasks`, `epics`, and `repos`; scopes combine as a union. `types`, `events`, and `reasons` intersect that scope. `exclude` accepts the same keys and exclusions win. Unknown keys, task types, event classes and preferences raise `ValueError`. An explicit task follow must overlap the role's owned event partition; broad repo/epic matching always remains constrained by effective ownership. Following never assigns ownership.

Preferences: `preset` (`quiet`, `normal`, `verbose`), `mode` (`profile`, `immediate`, `digest`, `muted`), `class_modes`, `digest_interval`, `digest_wake`, `detail_limit`, `minimum_severity`, `enabled`, `expires_at` (epoch), and `stop_on_completion`. Quiet retains actionable issues and digests routine work; normal additionally offers accepted research/investigation results and implementation/fix/docs landing; review/mechanical successes remain routine. Verbose offers stage/progress but remains subject to the queue cap. Explicit modes/class overrides supersede profile choices. Muted unresolved issues remain retained/readable. Digest schedule/detail settings are personal to the role; tuning one role does not advance another's coverage.

Semantic classification is version 1. `task.result-accepted` and `result.accepted` explicitly mean accepted results; worker `run-finished` remains routine. Existing `status` to `landed` is accepted delivery, `question-asked`/urgent comments need decisions, decision answers/records enter summaries, stalled/refused/exhausted recovery facts become issues, and recovery facts resolve matching causes. Internal `director.*` and `notification.*` events never route. No-op initial deps/related facts do not create routine noise. Envelopes carry schema/profile versions, source event, task/group/cause, actor/time/severity/reason, expected task event revision, evidence links/content and pinned subscription revision. Oversize evidence becomes a bounded excerpt plus a retained source-event reference.

## Persistence and transaction decisions

V9 appends these tables without editing V1–V8:

- `director_roles`: durable identity, generation/revision/state, policy and personal digest clock.
- `task_ownership`: unique `(task_id,event_class)` partition owner/revision. Exact class takes precedence over `*`. Ownership cascades on owner-approved task deletion.
- `subscriptions` and `subscription_versions`: stable subscription identity and event-positioned immutable selector/preference versions.
- `notification_intents`: one source event's semantic class, original subscription/profile snapshot and mutable current recipient/revision. Captured inside `ops.event` before Waiting resolves nested events. Historical migration events preceding V9 safely skip the hook.
- `notification_inbox`: unique source event, stable materialized identity, offered/acknowledged/resolved facts, issue identity, and digest coverage link. Delivery IDs are `notification:<integer>`.
- `notification_digests`: deterministic personal summary bodies and independent offer/ack fields; IDs are `digest:<integer>`.
- `notification_coverage` and `notification_compactions`: personal summary positions/counts and compacted facts grouped by task/event partition. Compaction is distinct from processing acknowledgement.
- `notification_positions`: shared durable projection cursor.
- `notification_escalations`: one visible owner storage question and scope admission hold per affected director.

Ownership transfers update both unprojected intents and materialized unacknowledged deliveries, including previously offered work, preserving delivery identity. Already acknowledged history remains unchanged. Pending compacted coverage also transfers by current effective event partition, so a successor's digest contains pending work and the old director's unsent digest excludes it. Pending offers recheck current ownership; historical summary records remain stored.

Routine inbox rows beyond the role cap are removed only after a durable coverage/resync record is written. Append-only source audit facts remain available. Repeated unresolved issue observations with the same task/class/cause coalesce into one retained issue entry with updated evidence; a resolved episode stops offers and a new episode gets a new delivery. Issues are independent of routine compaction. At actionable high-water, the existing owner Inbox receives one question, new assignment/submission for that role is refused, and other roles continue. Confirmed issue resolution below the mark releases that storage hold. Owner task deletion revokes pending offers while retaining audit state.

## TDD and validation evidence

Tests use isolated `/tmp` WAL databases and fake epoch time through the public `Notifications`/`ops` seam. No private tables are queried by the notification behavior tests. Initial public test was run before implementation and failed with `ImportError: cannot import name Notifications`; the first green proved immediate accepted completion, restart and ten disjoint directors.

Subsequent red/green slices covered missing health/tuning, selector behavior, transfer before projection and after offer, per-class delegation, independent digest timing and coverage, job profiles, offline compaction and issue preservation, payload/batch bounds, role defaults/stop conditions, compacted transfer, replay/preview, issue resolution, high-water release, owned task deletion, and durable ownership provenance. Each missing behavior produced the expected error/assertion failure before its implementation. The simulated clock failure during projection proves inbox insertion and cursor rollback together; retry offers both accepted results exactly once as stored identities.

Fresh successful verification:

```
DROVER_TEST_TMPDIR=/tmp python3 -m unittest tests.test_notifications tests.test_ledger tests.test_dispatcher tests.test_waiting tests.test_replans tests.test_replan_batches -q
```

Result: **267 tests passed**, including **17 notification tests**, in 20.063 seconds. Existing Python 3.14 `ResourceWarning` about an unclosed database is unchanged. `git diff --check` is clean.

An earlier expanded command additionally included `test_waiting_migration`, `test_waiting_structure`, `test_waiting_consumers`, and `test_waiting_characterization`: 327 tests ran, with three errors from existing imports (`fastapi` missing in one consumer API test, `markupsafe` missing in two UI consumer tests). The remaining 324 passed. Dependencies were not installed. The narrower earlier 201 ledger/dispatcher/waiting/replan run also passed.

## Explicit integration concerns

1. The baseline has neither task parent lineage nor group membership. Group and descendant selector keys deliberately reject instead of inventing semantics. Task 3 must extend the same matcher with real group enrollment/lineage and inherited ownership. This boundary was escalated to the root implementer.
2. Unowned source facts are durable `role_id=NULL` intents and counted by global `health()['unassigned']`. No blanket new question is added for every legacy unowned task. The owner health/Inbox adapter in Task 4 must surface these as ownership-resolution work. This preserves existing ledger caller/Inbox behavior and was escalated to the root implementer.
3. Storage admission is enforced at `assign`/initiated submission. Dispatcher claim/launch and action admission must call the current role admission fence during their later integration; the notification service does not become a second dispatcher.
4. Task 2 must fence acknowledgements/actions by role generation/lease and current effective ownership, transfer any handling leases separately, and preserve acknowledged history. Existing inbox fields support those additions; this increment grants no external credentials or action authority.
5. This increment does not validate interactive session push/resume, HTTP authentication, broadcasts, STOP/reload controls, supervised director runs, or group lifecycle. Those remain their planned tasks; no success is fabricated for any of them.
