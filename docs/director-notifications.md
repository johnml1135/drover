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
A role policy may explicitly grant `broadcast_summary`; it grants summary
notices only. A role named `owner` or `service` still has director authority.

The owner Inbox displays deduplicated unassigned issues and retained director,
group and task questions. Health displays controls, held reasons, pending issues,
and group readiness. Answering or commenting on a fallback question records an
owner decision; it does not reset budgets, accept child results, clear holds or
resume STOP. Director/group comments support ordinary discussion; urgent/replan
comments require a task or epic target.

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
Only retained deliveries addressed to the authenticated director and currently
authorized are replayed; a successor never inherits processed predecessor
history. Delivery is at least once: deduplicate by delivery identity/version and
use idempotent processing request keys.

A compacted replay range returns `resync_required`, a scoped ownership snapshot,
health and a fresh cursor; SSE uses an explicit `resync` event. Reconcile the
snapshot and current `/inbox`, then continue with that cursor. Replay batches are
bounded and can require multiple projection passes for a large source backlog.
The existing owner UI `/events` bus retains its original behavior.
