# Durable Subscriptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement selective durable director notifications, bounded supervised continuation, controls, and accepted-result joins without model polling.

**Architecture:** Extend the existing SQLite ledger with versioned notification, role, outbox, and group records. Keep a small public service facade over focused modules; HTTP/CLI and supervised adapters share its authority and transaction rules. Preserve the existing worker lifecycle and Windows hub ownership.

**Tech Stack:** Python standard library, SQLite, existing FastAPI/Pydantic hub, unittest; no new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-10-23-durable-subscriptions-design.md`, including Review resolutions and implementation contracts; ADRs 0008–0010.

## Global Constraints

- Support typically two to five independent directors and validate ten.
- Each ordinary event belongs to exactly one owning director; preserve explicit unassigned owner escalation.
- Initial digest interval 15 minutes; digest creation and dormant directors use no model tokens.
- Default recovery permits one dead-director replacement and two task retry/restart actions per issue, intersected with existing authority/admission limits.
- Do not deploy, merge, install dependencies, or operate T-b7. Tests use isolated state and zero model calls. The owner subsequently authorized controller-managed live tracking and bounded read-only dogfooding in Drover epic E-16; implementation/review agents must not mutate live state or launch paid agents themselves.
- Reuse the current linked worktree `design/durable-subscriptions`; append migrations without changing applied versions.
- HTTP/CLI first; signed webhooks and named pipes remain deferred. Unsupported interactive push/resume is visibly held.
- Automatic role execution is disabled until explicitly configured; deterministic subscription projection, digests, and controls run in ordinary hub code.
- Preserve task-types event/action-index outbox identities and owner Inbox compatibility; do not implement its unrelated registry/grill subsystem.

## Review Focus

- Late projection plus transfer must never recreate old-owner authority: Task 1 transfer/replay test.
- An event arriving as a role exits must preserve one pending wake: Task 2 checkpoint/release race test.
- Children completing before seal and group-only cycles must not strand a valid join: Task 3 seal/cycle tests.
- STOP racing with dispatch or merge must fence new effects and expose uncertain existing effects: Task 5 barrier tests.
- Legacy worker credentials and completion paths must not impersonate or complete a director: Tasks 4 and 5 credential/lifecycle tests.

## Files and boundaries

`drover/notifications/` owns the new public ledger service: `core.py` (roles/ownership/subscriptions and projection), `delivery.py` (receive/digest/retention), `roles.py` (session credentials, leases, checkpoints and scoped handling), `groups.py` (accepted joins), and `controls.py` (broadcasts and durable controls). `drover/ledger/actions.py` is the shared durable action-intent/receipt service. `drover/directors.py` bridges roles to dispatcher admission and supervised execution. `hub/notifications.py` and `drover/notifications/__main__.py` are transport adapters. Existing ledger/dispatcher/hub files contain only integration hooks and authority fences.

### Task 1: Durable ownership, subscriptions, projection and personal digests

**Files:** Create `drover/notifications/{__init__,core,delivery}.py`, `tests/test_notifications.py`; modify `drover/ledger/{schema,ops}.py`.

**Interfaces:** Public `Notifications(conn, now=None)` facade exposes `register(role_id, **policy) -> dict`, `assign(task_id, role_id, expected_revision=None, event_class='*') -> dict`, `follow(role_id, selectors, **preferences) -> dict`, `tune(subscription_id, expected_revision, **preferences) -> dict`, `project(limit=100) -> int`, `receive(role_id, limit=50) -> list[dict]`, `digest(role_id) -> dict | None`, and `health(role_id=None) -> dict`. Stable identities and JSON envelopes are returned; conflicting revisions raise a public conflict exception. Later tasks extend this same facade. All direct methods are trusted in-process services; transport callers require Task 4 authentication. `ops.create_task(..., initiating_role=None, group_id=None)` atomically enrolls ownership/follow before its first event. `ops.events(..., after=0, limit=None)` performs bounded SQL reads when a limit is supplied.

- [x] Write and run a failing public-service test for immediate completion, restart, and ten disjoint owners (`python3 -m unittest tests.test_notifications -v`).
- [x] Append migration for stable roles, ownership/delegation revisions, versioned subscriptions, routing intents, inbox/digest/coverage, and projection positions; implement event hooks and atomic task auto-follow.
- [x] Add/run tests for selector union/intersection/exclusions, unknown keys, profile defaults, future-only tuning, transfer before/after projection, digest timing/independent coverage, and routine retention compaction versus unresolved issue preservation. Implement only the behavior each test requires.
- [x] Verify notifications plus ledger/waiting regressions. Commit this independently useful increment.

### Task 2: Authenticated roles, processing commits, shared action outbox and scoped recovery

**Files:** Create `drover/notifications/roles.py`, `drover/ledger/actions.py`, `tests/test_director_roles.py`, `tests/test_actions.py`; extend notification facade/schema.

**Interfaces:** `issue_session(role_id, session_id, ttl=3600) -> dict` returns a one-time opaque credential and generation; `authenticate(credential) -> dict` validates hash/expiry/current generation; `acquire(role_id, session_id, ttl=60) -> dict` returns generation/lease token; `commit(role_id, lease_token, checkpoint, delivery_ids, actions, request_key, release=False) -> dict` atomically checkpoints/acks/enqueues, returning durable receipts; `claim_issue(role_id, issue_id, lease_token, **policy) -> dict` globally fences a single handler. `actions.enqueue(conn, event_id, action_index, kind, payload) -> dict`, `pending(conn, limit=32) -> list[dict]`, and `complete(conn, action_id, result) -> dict` share one outbox keyed by source event/action index. Different content at an existing identity conflicts. Destination task actions recheck ownership, generation, expected task state, and issue budgets.

**Processing identity:** A first `(role_id, request_key)` processing commit appends one internal `director.checkpoint` event and stores its event ID and canonical request hash in the processing receipt, within the checkpoint/ack/outbox transaction. `actions.enqueue` receives that event ID plus each action's zero-based request-order index. An identical retry returns that receipt and those same actions without another source event; changed content conflicts before any mutation. This also applies to commits with zero delivery IDs. Internal checkpoint events never route back into the inbox.

**Delivery versions:** `commit` additionally accepts `delivery_versions=None`, a delivery-ID-to-message-version map. Notification envelopes expose `message_version`. Processing an actionable/coalesced issue requires its observed version; reject a newer current source fact, including one not yet projected, rather than acknowledging unseen evidence. HTTP/CLI echo versions for all notification acknowledgements. Immutable deliveries may omit the map in trusted in-process calls. Receipt replay after a releasing commit uses the original request hash/session/generation and lease hash without requiring the removed execution lease or creating new effects.

- [x] Write/run failing tests for stale generation/credential rejection, competing issue handlers, atomic checkpoint/ack/action commit with rollback, and idempotent lost-response replay.
- [x] Append migration for credential hashes, leases, checkpoints, processing request receipts, issues and shared `action_outbox` records. Implement authority checks within write transactions; never persist credentials in audit/config/checkpoint payloads.
- [x] Test event-at-lease-release wake preservation, pause/cancellation, unresolved issue muting, durable two-attempt recovery budget and one-replacement budget, fallback deduplication, and reauthentication after replacement.
- [x] Verify Task 1 tests plus `python3 -m unittest tests.test_director_roles tests.test_actions -v`; commit.

### Task 3: Broadcast controls and sealed accepted-result groups

**Files:** Create `drover/notifications/{groups,controls}.py`, `tests/test_notification_groups.py`, `tests/test_notification_controls.py`; extend schema/event hooks/`ops.add_dep`.

**Interfaces:** `create_group(group_id, role_id, parent_id=None, mode='synthesis') -> dict`, `enroll(group_id, child_id, expected_revision, request_key) -> dict`, `seal(group_id, expected_revision, request_key) -> dict`; public group read returns membership, readiness, immutable manifest and failure reason. `broadcast(issuer, targets, body, urgency='summary', control=None, request_key=None) -> dict`; `stop(issuer, request_key) -> dict`, `resume(issuer, expected_epoch, request_key) -> dict`, `require_skill(issuer, skill, digest, targets, request_key) -> dict`, `ack_control(role_id, lease_token, control_id, digest=None) -> dict`; `admission(conn, role_id=None, generation=None, epoch=None) -> dict` returns allowed/reason/current epoch for worker and role paths. Global controls require owner/service authority.

**Lifecycle boundary:** This increment supports synthesis groups only. Reject collection mode explicitly until a declared accepted collection lifecycle exists; a group manifest never bypasses parent approval or acceptance.

- [x] Write/run failing tests for completed-before-seal children, concurrent completions/restart, cancelled/failed child rejection, immutable result manifests, synthesis parent incompleteness, and combined task/group cycle rejection in either enrollment or later dependency insertion order.
- [x] Append migration; implement accepted-success predicates against legacy merged/read-only accepted results, with explicit adapters for future lifecycle receipts. Never reuse generic terminal waits as successful joins. Group enrollment auto-follows owned children and suppresses individual final wakes by default.
- [x] Test summary/urgent broadcast snapshot targeting, quiet/muted bypass, independent acknowledgements, persistent stop/reload on new sessions, control authorization, wrong-version skill ack rejection, and idempotency. Implement deterministic control state without model invocation.
- [x] Run all notification tests plus waiting/dependency regressions; commit.

### Task 4: Scoped HTTP/CLI, durable replay and owner visibility

**Files:** Create `hub/notifications.py`, `drover/notifications/__main__.py`, `tests/test_notification_api.py`, `tests/test_notification_cli.py`; modify `hub/{app,auth,api,store,web}.py`, owner templates, narrow ledger target/schema hooks, the notification facade, and relevant CLI documentation.

**Interfaces:** Dedicated `/api/v1/notifications` router supplies role/session registration (owner only), follow/tune, task submit with auto-follow, receive/long-poll, processing commit, ownership transfer (owner only), group operations, broadcast/control operations and health. Director bearer auth is a distinct dependency; legacy API rejects these credentials without UI fallback. CLI `python -m drover.notifications` calls HTTP only, accepts a private credential file/environment reference, and supports follow/tune/inbox/commit/group/control with JSON output. Durable SSE uses ledger identities, scoped replay, explicit resync, and bounded batches; preserve `/events` UI compatibility.

- [x] Write/run failing API tests using isolated hub TestClient instances: two roles cannot read/ack/tune/act for each other, forged role text/shared worker bearer cannot register/issue/global-control, owner UI+CSRF can manage roles, and expired/replaced credentials fail.
- [x] Implement validated request schemas, bounded payloads, HTTP conflict/error mapping, polling outside write transactions, authenticated transactional service calls, and owner-visible held/unassigned issues through existing owner views.
- [x] Test CLI command payloads/output/credential redaction/no redirect, HTTP-to-ledger immediate task completion/restart, replay/resync and owner health; implement CLI and docs.
- [x] Run `python3 -m unittest tests.test_notification_api tests.test_notification_cli -v` in the existing hub environment plus API/auth regressions; commit. Existing Linux hub environment used real in-process ASGI with a test-only wake timer; real listener/Windows wire tests remain a Task6 environmental gate.

### Task 5: Dispatcher-supervised director runs and execution fences

**Files:** Create `drover/directors.py`, `tests/test_directors.py`, `tests/test_notification_fences.py`; modify `drover/{dispatcher,launch,landing,ci,origin,port,usage}.py`, `hub/{dispatch,services}.py`, and narrow ledger lifecycle/schema hooks as necessary. CI/origin hooks guard the actual launch and merge/push effects; standalone callers retain compatibility.

**Interfaces:** `DirectorRuntime(port, adapter=None, now=None)` exposes `tick(admission, capacity) -> dict`, `recover() -> dict`, `finish(run_id, result) -> dict`, and `stop_active(epoch) -> dict`. Adapter capability declaration distinguishes live offer, verified resume and fresh start; probe result is alive/gone/unknown. Default unsupported capability is explicitly held. A configured compatible runner adapter uses existing launch/nonce/adoption/stop mechanisms and shared allowance/seat accounting. Director-run association records role/generation/checkpoint/epoch; its archived accounting task never enters normal task claiming, attempts, worker completion or gates. Add optional runtime injection to dispatcher without changing legacy defaults.

- [x] Write/run failing tests for dormant zero-seat roles, coalesced wakes, resource/allowance refusal, one verified replacement, uncertain liveness hold, children continuing, stale-generation finish, run adoption after restart, and unchanged ordinary worker attempts/status.
- [x] Implement distinct director run start/finish/recovery paths and durable launch intents. Reconcile launch effects by nonce before retry; unknown effects hold. Service ticks project and digest deterministically even while paid execution is disabled.
- [x] Test STOP barriers between tick admission and launch, between group CI and merge, persisted stop across hub restart, nonce stop requests, and explicit uncertain termination. Fence shared action admission and relevant merge safe points, preserving already-issued effects for reconciliation.
- [x] Run notification, dispatcher/launch/replan/merge/usage regression suites; commit.

### Task 6: Final acceptance, review and operational handoff

**Files:** Update this plan and feature usage documentation; add `tests/test_notification_acceptance.py` only for cross-component scenarios not covered above. Diagnose and correct owner Inbox compatibility in `hub/store.py` / `drover/notifications/` with `tests/test_hub_stats.py` regressions. Record live work and issues under Drover epic E-16; see [dogfood tracking](2026-10-23-durable-subscriptions-dogfood.md).

**Interfaces:** Use the public service, HTTP/CLI and supervised-adapter seams above; no private database assertions as a substitute for observable behavior.

- [ ] Exercise every numbered spec acceptance scenario and review-resolution race; add missing cross-component tests with controlled time/adapters and zero model calls.
- [ ] Run the existing full test suite in its authorized environment, recording baseline/platform skips separately from regressions. Run formatting/reference checks and `git diff --check`.
- [ ] Dispatch a fresh whole-change review; fix important findings with regression tests, then rerun affected checks. Document exact supported adapter capabilities and configuration defaults.
- [ ] Attempt Windows-hub/WSL integration only through an isolated test process/database with available existing tooling. If unavailable, record this as an unverified deployment gate; do not claim interactive-session push has been proven.
- [ ] Mark verified steps complete, commit, and report branch/commits, test results, and remaining environmental verification limits. Leave integration/deployment to a separate authorized action.
