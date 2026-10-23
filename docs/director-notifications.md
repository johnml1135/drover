# Director notifications over HTTP

The hub owns the ledger. Directors use `/api/v1/notifications` or
`python -m drover.notifications`; the client never opens a database. Notification
projection, personal digests and controls use no model calls. Automatic director
execution remains disabled until a compatible supervised adapter is configured.

## Owner setup

Use an authenticated owner UI request on the UI listener, with same-origin CSRF
metadata or a matching Origin. A shared worker bearer and director credentials
cannot administer roles, issue sessions, transfer ownership or publish global
controls. An explicit invalid, expired or revoked bearer never falls back to
owner UI authority, including on loopback.

Owner JSON endpoints (relative to `/api/v1/notifications`):

| Method/path | Body |
| --- | --- |
| `POST /roles` | `{"role_id":"research","policy":{}}` |
| `POST /roles/research/sessions` | `{"session_id":"research-session","ttl":3600}` |
| `POST /ownership` | `{"task_id":"T-1","role_id":"research","expected_revision":1}` |
| `GET /owner/health` | none |
| `POST /owner/groups` | `{"group_id":"join","role_id":"research","parent_id":"T-1"}` |
| `POST /owner/groups/join/enroll` | `{"child_id":"T-2","expected_revision":1,"request_key":"enroll-1"}` |
| `POST /owner/broadcast` | `{"targets":"all","body":{"message":"update"},"urgency":"urgent","request_key":"notice-1"}` |
| `POST /controls/stop` | `{"request_key":"stop-1"}` |
| `POST /controls/resume` | `{"expected_epoch":1,"request_key":"resume-1"}` |
| `POST /controls/skill` | `{"skill":"review","digest":"sha256:...","targets":["research"],"request_key":"skill-1"}` |

Session issuance returns the credential once. Save it in an owner-controlled
private file or environment reference; do not put it in command arguments,
checkpoints, logs or task descriptions. Reissuing a session revokes the old
credential and execution lease. Session capabilities may narrow the default
`notifications.read`, `notifications.process`, `notifications.tune` set.
Nested role policy and notification preferences reject unknown fields and invalid
types before calling the ledger. Supported policy fields are `notifications`,
`max_pending_routine`, `max_pending_actionable`, `broadcast_summary`,
`action_allowlist`, `allowed_models`, `recovery_attempt_limit`, `replacement_limit`,
`wake_limit_per_hour`, and `token_budget`. Runtime configuration must use its
declared configuration seam; future policy fields require an explicit schema
extension.

A role policy may explicitly grant `broadcast_summary`; it grants summary
notices only. A role named `owner` or `service` still has director authority.

The owner Inbox displays deduplicated unassigned issues and retained director,
group and task questions. An existing question or needs-owner task keeps its
usual card; its notification fact does not add another card. Archived tasks hide
their unassigned issues until restored. Separate unresolved recovery causes stay
visible independently, and assigning one event class leaves other unassigned
classes with the owner. Health displays controls, held reasons, pending issues,
and group readiness. Answering or commenting on a fallback question records an
owner decision; it does not reset budgets, accept child results, clear holds or
resume STOP. Director/group comments support ordinary discussion; urgent/replan
comments require a task or epic target.

Defaults for a newly registered role and its subscriptions:

| Setting | Default |
| --- | --- |
| Notification profile | `normal`, `mode=profile`, minimum severity `info` |
| Personal digest | Every 900 seconds, at most 50 detailed entries, `digest_wake=false` |
| Subscription lifetime | Enabled, no expiry, `stop_on_completion=false` |
| Pending routine/actionable limits | 1,000 / 10,000 per role |
| Processing session / lease lifetime | 3,600 / 60 seconds |
| Task action allowlist / allowed models | Empty / empty |
| Wake / model token budget | 4 wakes per hour / 0 tokens |
| Issue recovery / automatic replacement limit | At most 2 attempts / 1 replacement |

The normal profile sends issues and decisions immediately. It sends accepted
research/investigation results, group readiness and most accepted landings
immediately; routine stages enter the personal digest. Joined child results enter
the digest while the group's accepted completion produces the final wake.
Empty digest intervals produce no message. Quiet/verbose profiles, event-class
overrides and explicit modes change delivery preferences within existing
authority; they cannot grant task access or actions.

## Director CLI

Set `DROVER_HUB_URL` or pass `--url`. On Unix, a credential file must be a regular
file owned by the invoking user with no group/other permissions. On Windows,
protect its ACL before use; an environment reference also avoids a token file.
A file may contain a raw token or the session-issuance JSON. The client refuses
redirects and reports JSON errors without remote response bodies or credentials.

```sh
export RESEARCH_CREDENTIAL="$(cat /private/research-token)"
python -m drover.notifications --credential-env RESEARCH_CREDENTIAL health
python -m drover.notifications --credential-file /private/research-token lease '{"ttl":60}'
python -m drover.notifications --credential-env RESEARCH_CREDENTIAL submit '{"title":"Research finding","kind":"research"}'
python -m drover.notifications --credential-env RESEARCH_CREDENTIAL follow '{"selectors":{"tasks":["T-1"]},"preferences":{"preset":"normal"}}'
python -m drover.notifications --credential-env RESEARCH_CREDENTIAL tune S-1 '{"expected_revision":1,"preferences":{"mode":"muted"}}'
python -m drover.notifications --credential-env RESEARCH_CREDENTIAL inbox --wait 30 --limit 50
```

JSON payload arguments also accept `@file.json` or `-` for stdin. Keep execution
and issue lease tokens in private payload files or stdin. `submit` atomically
assigns ownership and follows the task before its first event. `parent_id` and
`group_id` require current authority; a group reference grants no authority over
another director's child task. Ordinary task approval remains an owner decision.

Acquire `/lease` before processing. Copy each delivery's `delivery_id` and
`message_version` from the inbox into the commit. A commit atomically saves the
checkpoint, acknowledges those observed versions and accepts scoped actions.

```json
{
  "lease_token": "TOKEN_FROM_LEASE",
  "checkpoint": {"next_step":"synthesize"},
  "delivery_ids": ["notification:17"],
  "delivery_versions": {"notification:17":42},
  "actions": [],
  "request_key":"research-step-1",
  "release":true
}
```

Run `commit @private-commit.json`. Identical retries return the original receipt,
including after release; reusing a request key with different content conflicts.
Newer coalesced or unprojected evidence rejects a stale acknowledgement. Fetch
again and inspect the new version. Compacted identities require resync; they never
acknowledge a replacement delivery. Use `checkpoint` to retrieve the latest save.
Issue recovery additionally uses `issue claim ID PAYLOAD` and `issue hold ID
PAYLOAD`; policy, ownership, revision and recovery limits still apply.

`group create ID PAYLOAD`, `group get ID`, `group enroll ID PAYLOAD`, and
`group seal ID PAYLOAD` use revisioned membership and request keys. Directors
may enroll only their own children; an owner may explicitly establish a join
across directors. The join publishes accepted result references and readiness,
without approving the synthesis parent or granting foreign child read/action
scope. Open, failed or incomplete joins show their readiness reason.

Use `control state` to inspect retained requirements and `control apply ID
PAYLOAD` with `lease_token` and the exact required `digest` to record application.
Delivery acknowledgement and control application are distinct. Acquire remains
available under STOP so saving and application can proceed. After a control
epoch change, release and reacquire before ordinary actions. Director lease
`control_epoch` is actor-effective; worker admission uses the global epoch.
Owner resume must echo `global_epoch`, not total `sequence` or `role_epoch`.
Application does not assert that an existing external process has stopped.

## Durable replay

`GET /inbox` returns at most 100 current pending offers. `wait` is 0–30 seconds;
waits occur outside write transactions and revalidate the bearer on each poll.

`replay --after v1:42:3 --limit 50` calls `/replay`. `/events` supplies a bounded
SSE batch with the same semantics and accepts `Last-Event-ID`. Cursors contain a
persisted source-event position and digest identity. Each event carries the
stable delivery identity and its latest `message_version`. The final
`checkpoint` SSE event carries `more`; reconnect or fetch another batch after
processing. Pending offers repeat until committed even when their source is
older than the cursor, including after ownership transfer or late projection.
Historical notification pages follow source version order, and digest pages follow
digest identity order. Pending reoffers do not jump over older retained history.
Only retained deliveries addressed to the authenticated director and currently
authorized are replayed; a successor never inherits processed predecessor
history. Delivery is at least once: deduplicate by delivery identity/version and
use idempotent processing request keys.

A compacted replay range returns `resync_required`, a scoped ownership snapshot,
health and a fresh cursor; SSE uses an explicit `resync` event. Reconcile the
snapshot and current `/inbox`, then continue with that cursor. Replay batches are
bounded and can require multiple projection passes for a large source backlog.
The existing owner UI `/events` bus retains its original behavior.

## Supervised execution

Dispatcher and service ticks project at most 100 source events, build deterministic
digests, and execute at most 32 accepted local actions. Those operations run even
when automatic director execution is disabled. Action admission and destination
checks still obey dispatch pause, STOP, role generation and scoped authority.

Automatic execution defaults off. The trusted settings seam is
`director_execution_enabled=true` plus `director_execution`, a JSON mapping from
role ID to exactly `backend`, `model`, `seat`, `max_tokens`, `placement`, and `cwd`.
These are runtime settings, separate from the owner notification Policy schema.
Role policy must explicitly allow the model and provide token and wake budgets.
The dispatcher applies its existing account allowance, reserve, seat, platform,
resource and parallel capacity checks. Open director runs count across restart
and uncertain termination; dormant roles consume no seat.

`DirectorRuntime(port, adapter=None, now=None)` provides `tick(admission, capacity)`,
`recover()`, `finish(run_id, result)`, and `stop_active(epoch)`. Tick takes a global
admission snapshot and a capacity object with `slots` (integer or current-count
callback) and `admit(spec)` (returns a refusal reason or `None`). Hub construction
injects it through `DispatchRuntime.directors` into the one locked dispatcher.
Tick reports `capacity_used` for new acknowledged or possibly-issued launches;
the dispatcher charges both before admitting more work. `launched` lists only
acknowledged launches. Services perform deterministic work
only and never launch a second supervisor.

Adapters declare `live_offer`, `verified_resume`, `fresh_start`,
`enforced_token_cap`, `authenticated_processing`, and `secure_handoff` booleans.
They implement `launch(spec, credential, guard)`, `probe(spec)` returning
`alive`/`gone`/`unknown` for the exact nonce, `adopt(spec)`, and `stop(spec)`.
An optional `verify_resume(previous_spec)` must positively verify compatibility
before `mode=resume` is selected; otherwise a supported fresh run is required.
Live offers are capability metadata; this increment makes no interactive push
claim. An active invocation reads its durable inbox and coalesces new wakes.

The credential is handed off once, separately from the durable spec; it remains
a scoped bearer until expiry or revocation. A compatible adapter must enforce
the reserved token maximum, preserve the read-only director sandbox, securely
deliver credentials through a private file/pipe or equivalent, and perform the
authenticated acquire/checkpoint/acknowledgement protocol. It calls `guard()` at
its actual launch boundary. It may raise `AdmissionHeld` only before an effect
was issued; other launch errors retain an unknown-effect hold for reconciliation.
Credentials must never enter argv, logs, durable specs, receipts or checkpoints.

`RunnerDirectorAdapter(launcher, executor=None)` bridges the existing runner's
nonce, scan, adoption and stop-request mechanisms. A compatible injected executor
implements `launch(launcher, spec, credential, guard)` and optional
`verify_resume(launcher, previous_spec)`. Existing model CLI launchers do not
provide verified hard token-cap/authenticated-processing integration; the default
adapter therefore reports an explicit unsupported hold and does not launch.
Enabling the setting alone cannot supply these missing capabilities.

The supported default is durable HTTP/CLI consumption with deterministic
projection, digests and local accepted actions. A consumer must save a checkpoint
and commit delivery versions before it considers work processed. Hub restart
preserves pending offers; a disconnected consumer's replacement restores that
checkpoint and unread work under a new generation.

The acceptance suite verifies restart and replacement with an isolated real
zero-model subprocess whose private pipes proxy authenticated requests into the
real ASGI routes. This controlled adapter does not establish production model CLI
capabilities, interactive-session push, or a Windows-to-WSL network route.
Deployment still requires a compatible executor with the capabilities above and
an independently verified hub endpoint reachable from the worker environment.

Director accounting uses an archived internal task and dedicated run associations;
normal task/run APIs cannot impersonate this lifecycle. Launch and stop intents
share the event/index `action_outbox`, with internal kinds excluded from role
allowlists and deterministic task execution. Lost launch responses are reconciled
by nonce before replacement. Unknown liveness consumes capacity and never proves
death. Observer errors and starting timeouts also retain the run until its nonce
has verified terminal evidence. One automatic replacement is allowed by default;
an exhausted failed run remains held through restart and new source notifications. Checkpoints, budgets,
child task attempts, statuses and gates are preserved.

STOP is checked at claim, intent acceptance, actual detach, group CI launch, and
final merge/push boundaries. Active runs get nonce-addressed stop requests;
request receipts explicitly leave termination unknown until supervision observes
cessation. Stop/resume epochs also fence work admitted before an intervening STOP
that occurred between ticks. A local merge already issued is recorded as landed
even if a later STOP holds its push. Resume does not rewrite an old effect's
epoch or make an uncertain effect disappear.

Group CI saves whether its context is prepared or possibly issued. An uncertain
execution retains its original nonce and checkout across STOP/resume, even when
status is missing. Its existing acquired CI ticket is pinned against expiry
until terminal reconciliation explicitly releases it; ordinary queued/gate
ticket expiry is unchanged. Saved requests expose `held_reason` for uncertainty.
A legacy execution whose ticket already expired is held for explicit
reconciliation rather than launched again. Unresolved uncertainty can therefore
hold CI capacity indefinitely; resume alone does not release it.
