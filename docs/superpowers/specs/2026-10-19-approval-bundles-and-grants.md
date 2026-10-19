# Approval by decision bundle, standing grants, and deterministic queue control

Date: 2026-10-19. Task: T-ca. Status: implementation specification for owner review; no runtime authority is granted by this document.

## Purpose and decisions already made

The owner should decide a question once and see that decision take effect everywhere it applies. Today the owner approved 135 tasks through a lead-written shell script, answered numbered docket questions that the lead mapped to task IDs manually, and still had a worker block twice for “D1 approval missing” on the PyYAML task. Reprioritizing a systemic fix required moving it into the highest-ranked epic. Approval and ordering must become ordinary product actions with visible evidence.

The owner requested this spec and approved ranking decisions R1–R5 on 2026-10-19. Preserve the three approval layers, lead queue control, ledger-only score, and OP1/OP2/OP3 behavior below. Formulas, field names, and endpoint payloads make those decisions implementable; numerical defaults beyond the owner's term weights are proposed v1 policy defaults, subject to review with this spec. They are not additional historical owner decisions.

Deliver decision bundles first. Standing grants and epic approval build on the same decision transaction; ranking and overrides can land incrementally behind independent feature flags. This is a docs-only deliverable. It does not approve tasks, create grants, set overrides, publish, install, or change a live queue.

### Existing contracts and dependency boundary

Build on E-u's unstick/docket work: T-bl (assessor), T-bm (docket integration), T-bh (decision propagation), T-bg (unstick ladder); T-8j (batch approve); and T-8i (owner CLI credential). D5 here means the owner CLI authentication decision in [the task-types spec](2026-10-17-task-types-design.md), not D5 in the separate PR spec. E-v is the self-improving harness consumer.

The task worktree and supplied read-only main snapshot contain the task-types/PR designs but no unstick/docket design artifact or implementations for those task IDs. Treat their roles above as integration requirements from this task, not verified existing APIs. Before each implementation slice, reconcile its landed dependency interfaces; adapt inside that slice's authorized scope or report the mismatch. Do not invent completed prerequisites.

Relevant current seams:

- `drover/ledger/ops.py`: transactional mutations, `approved_at`, holds, and `ready_tasks`; the latter currently sorts priority, epic rank, task rank, pressure, size, ID.
- `drover/ledger/schema.py`: append-only forward migrations; `questions`, model-assessment `decisions`, `task_events`, tasks/dependencies/runs already exist.
- `hub/api.py`, `hub/auth.py`, `hub/store.py`: `/api/v1`, owner-only approval and reorder, bearer reads/proposals. Current epic approval loops over proposed tasks without an exclusion manifest.
- `drover/brief.py`, `hub/templates/plan.html`, `drover/dispatcher.py`: worker context, plan display, admission/claim loop.
- Task-types' `perform_action(principal, target, action, payload, expected_revision, request_key)` and owner-answer compatibility mapping are the common mutation boundary. Reuse T-bm's docket identity and T-8j's batch validation; do not introduce a second owner queue or identity system.

## Plain-language owner page

**Decide once.** Open **Decisions** on the website, or run `dv docket` after pairing your CLI. Each question shows the tasks it unlocks and the exact changes it will make. Pick an answer or write your own. Review the resulting changes and submit once. All the changes happen together, and every affected task shows your question, answer, name, and time. A worker can see “D1: yes, approved by owner” without finding the original conversation.

**Let routine work proceed.** In **Standing grants**, choose a lead or the unstick ladder, repositories, task kinds, sizes, risks, and a daily limit. For example: “docs, investigation, mechanical, and S/M fixes in these solo repos, up to 30 each day.” Each use appears in your daily digest. **Revoke** takes effect immediately for future uses. A grant cannot authorize external posting, installs, deletes, registry/system changes, or high-risk work.

**Approve an epic safely.** **Approve epic** shows included tasks and a separate excluded list with reasons. Tasks that post externally, install, delete, change registry/system state, or have high risk start excluded and need their own decision. Adding a task to the epic later does not approve it. You can also leave out any ordinary task.

**Move work.** Ask the lead to move a task; it uses the queue API and records why. The plan shows the resulting position, score, and “N tasks wait on this.” You can pin a task's manual position or forbid lead moves. A normal move adjusts the score inside your priority band; **Owner first (OP3)** puts work above the ordinary queue. **Stop the line (OP2)** takes the next eligible claim and freezes new work in its repo, or globally if selected. **Emergency (OP1)** takes the next dispatch opportunity on the best eligible seat, can use one extra parallel place, and bypasses a disk queue warning. Running work is never cancelled. Missing approval, prerequisites, unavailable seats, a hard resource stop, or your dispatch pause still show an explicit wait reason.

You can set any override. Agents may set OP3; emergency and stop-the-line changes by agents alert you immediately and expire after four hours unless you confirm them. The plan shows the timer. Landing or cancellation clears the override automatically. You can change or clear it at any time. Scores explain ordering; they do not give permission to perform an action.

## Authority and invariants

A principal is authenticated server-side, with stable identity and role. Do not infer owner authority from a payload's `actor`, a token's spelling, comments, free text, worker output, or a lead's report of what the owner said. T-8i/D5 supplies a separate expiring, revocable owner credential through explicit browser pairing. Keep web CSRF protection. CLI uses the hub API, never SQLite or spoofed UI headers; no owner credential enters a worker brief/environment.

“Lead” below denotes the owner's designated coordinating principal, not a new worker role. Scoped lead and unstick capabilities are bound to authenticated identities; a generic worker bearer does not become a lead because it supplies that name.

| Action | Owner | Lead / unstick / worker authority |
| --- | --- | --- |
| Propose a docket bundle, preview, read permitted decisions and scores | Yes | Within existing read/proposal scope; no approval effect |
| Submit an owner docket answer or explicit individual approval | Website or paired owner CLI | Never impersonate owner; a grant use is separately attributed |
| Create, widen, renew, revoke a standing grant; change its cap | Owner only | May propose; cannot issue or delegate grants |
| Approve a task under a standing grant | Yes | Only named grantee, matching all predicates, cap, and allowed action set |
| Approve an epic manifest or exceptional task | Owner only | May prepare preview; cannot use epic action to evade grant caps |
| Change priority bands, manual pin/move policy, or ranking policy | Owner only | May propose; grant does not confer policy authority |
| Reorder tasks within the owner-set band | Yes | Designated lead bearer capability, unless pinned/forbidden; reason required |
| Set/clear OP1, OP2, OP3 | Any level/duration, confirm agent override | Agents/lead: OP3 freely in task authority; OP1/OP2 require reason, immediate owner alert, maximum four hours; cannot clear/alter an owner-confirmed override |
| Verify answer compliance or attach stuck-task evidence | Yes | Assessor / unstick capability, no new approval authority |

A standing grant **never** covers posting PRs or responses (or other external publication), package installation or dependency addition, deletes (files, branches, worktrees, records, or remote resources), registry changes, system-state changes, or `risk:high`. Unknown or mixed effects fail closed. An innocuous kind/title is not evidence of harmless effects. Owner approval of exceptional work must specify that action's target and bounds; it cannot bypass repository/platform rules or authorize a forbidden operation.

Task approval and action authorization are separate. Even an owner-approved task cannot publish or install without the specific effect authorization required by its action service. This spec does not widen the PR spec's publication trust contracts; these grants cannot be used as PR trust entries. Scoring, a queue move, an override, or approval of an epic's ordinary subset never authorizes an excluded effect.

## Layer 1: decision bundles (land first)

### Bundle contents and lifecycle

A docket item is the unit of decision. Attach one immutable versioned bundle to the existing docket/owner-answer item, containing:

- Stable question ID and displayed number, full question/context, option IDs and labels, free-text permission, required fields, source spec/task references, and the proposed recommendation.
- Exact task membership with role (`unlocks`, `edits`, or `context`); all tasks changed or unlocked must be linked to the answer. Context-only tasks receive a clearly non-authorizing link.
- For each option, typed effects: explicit task approval, dependency add/remove, scope replacement, priority change, handoff text update, and any separately authorized queue/override action. Include before/after values, target revisions, and expected approval/hold predicates. No executable shell fragment.
- Approval-relevant fingerprints covering repo, kind/type version, risk, size, write/effect capabilities, file/read scope, acceptance/goal, dependencies, and action targets. Hash canonical JSON; hash is a binding, not a substitute for authorization.

Lifecycle: `draft -> open -> applied`, with `superseded`/`withdrawn` exits before application. An edited question, option, membership, or effect creates a new revision and supersedes the old proposal; it never rewrites an answered version. Reject expired or superseded forms with a diff and reload path. A decline/“no” is itself an answer with its specified effects (often no approvals); it must not accidentally approve the whole membership.

Support both stable option choices and full free text. Store the original text, without interpreting it as code or granting implied permissions. Structured supplemental text, such as a bounded path or handoff sentence, is validated against the versioned answer schema and rendered into an exact effect preview. If arbitrary text changes the proposed effects (“yes, except task X”), save an answer draft and have the assessor/lead propose a revised typed bundle. The owner sees and submits that final answer and effect set together. No LLM interpretation may run after final submit and silently alter the approved effect set. A free-text answer whose predeclared effect is simply “record these directions” may commit immediately; it unlocks only explicitly listed work, never guessed approvals.

### One transaction, one decision

Both website and `dv docket` call the same action service:

1. Authenticate principal and action, validate `expected_revision`, bundle hash, task fingerprints/revisions, grant if applicable, and idempotency key. Acquire the hub write transaction and recheck all predicates there.
2. Validate the complete typed effect plan, including dependency cycles, authorized scope/priority changes, task states, handoff length, exclusions, and unrelated holds. Grant-backed effects are limited to approval and releasing the exact approval hold; edits to scope, dependencies, priority, or handoff require an owner bundle or separate existing authority.
3. Persist one immutable decision record with the submitted question/options snapshot, answer, principal, authority basis, timestamp, bundle revision/hash, and full effect receipt. Attach it to every affected task. Apply **all** changes and approval stamps, then release only holds whose exact decision requirements this record satisfies. Preview hashes bind both the before-state preconditions and the computed after-state; approval links bind the **after-effect** fingerprint, so approved scope/dependency edits do not immediately invalidate their own approval.
4. Recompute eligibility through the existing state kernel. Approval does not force `ready`: incomplete dependencies, other holds, cancellation, stale approval, and existing execution state still apply. Do not requeue a running/gated/reviewed task to “unlock” it.
5. Append the canonical decision event, per-task references, and durable notification/assessor outbox entries in the same transaction. Commit, then publish SSE/inbox updates. No network call or model assessment is inside the transaction.

Any invalid member rolls back the answer, links, task edits, approvals, counters, events, and outbox. A retry with the same principal/request key and identical payload returns the original receipt; a different payload with that key conflicts. Two channels racing on the same revision produce one applied decision and one stale response, never two approvals. A post-commit delivery failure is retried from the outbox, not by reapplying the answer.

Keep questions and their task-types owner-answer mappings as a compatibility facade. Existing answer endpoints delegate here when a bundle is present. A multi-question docket round can contain several bundles; each explicit bundle submit is atomic. A “submit round” operation must preview a combined conflict-free bundle if it promises all-or-nothing behavior; it cannot disguise sequential partial commits.

### Worker and reviewer evidence

Task detail, API, reviewer context, fresh briefs, and resumed-worker context include a **Decisions applying to this task** section before trimmable discussion. Show decision ID, question ID/number and full relevant question/answer, who/when, authority (`owner` or `grant G-x used by principal`), task-specific effects, constraints, applicability fingerprint, and supersession/revocation status. An assessment ID is distinct from an owner decision ID. Preserve complete text in the task projection; if the brief cannot fit required evidence, use a dedicated readable decision artifact and pause launch until it can be supplied. Do not silently truncate the authorizing answer.

Launch/relaunch resolves current requirements from ledger links rather than searching transcripts. Resume delivers a new decision revision before dependent work continues. If T-bh cannot reliably inject into an active session, finish unrelated authorized work or hand off to a fresh brief; do not assume the session saw SSE. A worker reporting “approval missing” triggers a requirement lookup; a matching current record is supplied, while a mismatched/absent record creates one deduplicated docket issue. A lead assertion alone cannot satisfy the lookup.

T-bl assesses simple instructions against the task's outputs and exact decision effects: for example, “use the approved PyYAML approach, only this scope.” It records satisfied/violated/unknown plus evidence. Assessment cannot reinterpret an answer into broader authority, override an owner refusal, or manufacture an approval. Violations/unknowns enter the existing unstick/docket flow, with the original decision visible.

## Layer 2: standing grants

A grant is a scoped, revocable approval delegation to a named lead or the unstick ladder. It is not an owner credential. It has owner issuer, grantee, registered repo allowlist and collaboration mode, task kinds/type versions, size allowlist, risk allowlist (low only in v1), explicit allowed actions (`approve-task` and its exact approval-hold release), daily cap, effective/expiry times, and revision. Predicates are conjunctive; alternatives are explicit clauses, not accidental products. The example grant is `(kind in docs/investigation/mechanical) OR (kind=fix AND size in S/M)`, AND named solo repos, low risk, no forbidden effects, at most 30 task-version approvals per owner day. Each clause also states its size allowlist.

Use registered repo IDs and a server-owned collaboration classification; missing solo/team information makes a solo-only grant ineligible. Effect classification derives from pinned task/action metadata, including composite capabilities. Missing metadata excludes the task; neither the lead nor worker can label effects harmless to gain approval. Reclassifying or changing approval-relevant inputs invalidates the approval fingerprint and requires a new decision. If new forbidden effects appear, hold before execution; recheck at admission and effect execution.

For a use, atomically recheck active grant revision, grantee, current task fingerprint/predicates, and daily remaining cap, then create a grant-attributed decision and increment usage. Count task-version approvals, not API requests: a bundle approving 8 tasks consumes 8. Duplicate retries consume zero extra; already-approved unchanged tasks consume zero and return their original receipt. Reapproval of changed work consumes another unit. Multiple simultaneous uses cannot exceed the cap; usage sums across revisions of the same grant/day, so an edit cannot reset it. Different grants cannot be combined to satisfy one task's missing predicates.

A day is a calendar day in the owner's configured IANA timezone, fixed into each grant revision (America/New_York for today's example). Store its UTC start/end and timezone; daylight-saving days are not assumed to be 24 hours. Changing timezone cannot reset the current cap; owner replaces the grant for the next day. Revocation and use serialize in the same ledger transaction: no use committed after revocation is valid. Revoke is one click with no new approval prompt. It prevents future uses; existing valid task approvals retain their exact bounds and audit links. The owner can separately hold/revoke a task approval, and a suspected compromised grant can offer an explicit reviewed list to hold, without stopping running processes implicitly.

Every use and refusal is auditable. The daily digest groups uses by grant, repo, and task with decision links, remaining cap, failures, and excluded work. Immediate revocation invalidates caches for future grant uses; each use checks the authoritative current revision. A claim under an existing approval validates its fingerprint and that its grant was valid at the recorded use, without retroactively invalidating it on later grant revocation. Grant expiry behaves as revocation for future uses. Widening, renewal, higher caps, and a replacement grantee are owner-only new revisions, with no implicit renewal.

## Layer 3: epic approval with exclusions

Preview an immutable manifest of the epic's currently proposed/draft tasks, each fingerprint/revision, ordinary included subset, owner-optional omissions, and mandatory exclusions with **all** reasons: external posting, installation/dependency change, delete, registry/system change, high risk, or unknown effects. Never derive permission from epic membership alone. Already-approved tasks appear as unchanged context, not fresh approvals; cancelled/landed tasks are not resurrected.

Default all mandatory exclusions to unselected. They cannot be selected through the bulk action: open an individual decision with exact exceptional effects instead. The owner may omit more ordinary tasks. Show totals, dependency consequences (an included task may still wait on an excluded prerequisite), and changed task details before submit. T-8j applies the exact ordinary subset as one decision transaction. Any changed member/epic membership invalidates the manifest and returns a refreshed preview; a task added later needs another approval. An empty included subset is a no-op receipt, not approval of exclusions.

An epic is a convenient way to prepare a bundle, not a standing grant over future tasks. The lead may prepare it but cannot approve it with a bearer. A named grantee can approve matching individual tasks through normal grant use with per-task cap consumption; it cannot call epic approval to skip exclusions or caps.

## Data model delta

Append migrations after the actual landed schema and prerequisite migrations; do not assign migration numbers in this spec or modify applied migrations. Use existing task-type action/outbox/revision facilities when available. JSON below means canonical, schema-validated JSON, not an executable payload.

| Table / field | Required data and constraints |
| --- | --- |
| `decision_bundles` | Stable ID, docket/owner-answer ID, version, state, question/options/answer schema snapshot, member/effect plan JSON and hash, base revisions, proposer, created/expiry; unique `(id, version)`, at most one open current version per item |
| `bundle_tasks` | Bundle ID/version, task ID, role, expected revision/fingerprint; unique bundle/version/task; foreign keys and task lookup index |
| `approval_decisions` | Append-only ID, bundle/version, authenticated principal ID and role, authority basis, nullable grant ID/revision, question/answer snapshot, exact effects/results, timestamp, request key and payload hash; unique principal/request key. Keep existing model-assessment `decisions` separate |
| `task_decision_links` | Task ID + approval decision ID + requirement ID, relation (`authorizes`, `direction`, `context`, `supersedes`), applicability fingerprint; indexed by task and requirement. This is the many-to-many decision-record link on tasks; a single “latest answer” field is insufficient |
| Task approval projection | Retain `approved_at`; add/reference authoritative approval decision and fingerprint. Legacy approvals have explicit legacy provenance, never invented answers. Requirement satisfaction derives from links and current fingerprint |
| `standing_grants` | ID/version, issuer/grantee, predicate clauses/action allowlist, timezone/cap, effective/expiry/revocation times, reason, current revision; owner-only revisions, no transferable credential |
| `grant_uses` | Append-only grant/revision, decision, task, task fingerprint, day bounds, timestamp; uniqueness by decision/task; indexed grant/day. Serialized usage total enforces cap |
| Effect classification | Pinned task/action effect set, source policy/type hash, and `known` flag; server-derived. Store with task revision, invalidate on material edits |
| Queue control projection | Owner-set band (v1 P0/P1/P2/P3, legacy P4), move permission, manual rank/signed adjustment, pin and reason; scope policy revision. `tasks.rank`/`epics.rank` remain available for migration/UI, not comparator keys |
| `task_overrides` | One current override per task: level, reason, principal/role, set time, scope (`repo:<id>` or `global` for OP2), expiry, confirmed-by/time, incident ID, revision; append-only history in events. Index expiry/scope |
| `task_rankings` | One current per-task projection plus retained decision snapshots: score/breakdown, raw inputs and evidence IDs, wait count, effective override, eligibility/freeze reasons, ranking policy hash, ledger watermark, `as_of`, generation ID; API task expansion references it |
| `unstick_fix_links` | Reuse T-bg's canonical stuck-task/fix-task evidence link, source incident, active/resolved status and timestamps; unique active pair, reverse index. Do not add fake blocking deps to increase score |
| Policy / outbox | Versioned ranking weights/likelihood table, grant/queue capability bindings; durable notifications keyed by source event and destination, delivered-at and retry status. Reuse E-v/task-types stores |

Protect approval decisions, uses, and their history from update/delete. Superseding, revoking, correction, or assessment writes a new linked record/event. Task archival cannot erase evidence. Server-owned IDs, fingerprints, approval stamps, ranking values, classification, and actors are not accepted in ordinary task patch/status/event input. Generic event writes cannot become authoritative decisions or overrides.

## Deterministic ranking

### Eligible work and the comparator

Replace `ready_tasks`' old sort with exactly:

`(effective_override_order, -score, task_id)` where OP1=0, OP2=1, OP3=2, none=3; task IDs compare as canonical ASCII strings.

There is no additional priority, epic rank, manual rank, pressure, size, insertion-time, or seat-dependent tiebreaker. Eligibility remains separate: current approval, valid dependencies, holds, scope conflicts, platform/repo policy, and OP2 freezes are enforced before claim. Store a breakdown for all nonterminal tasks so the owner can inspect blocked and finishing work too. Gated/reviewed work continues through gate/review/landing queues; do not start a second worker just because its score is high. Finish-first informs any shared admission choice among already valid stages, preserving stage prerequisites and the landing queue.

A pure function `score_task(task_snapshot, graph_snapshot, policy_snapshot, as_of) -> breakdown` reads no DB, clock, live seat usage, network, randomness, or mutable config. The hub assembles all inputs from one ledger snapshot, including allowance evidence in `policy_snapshot`. Time and allowance readings are explicit ledger evidence. Use 50-digit decimal arithmetic with round-half-even for intermediate calculations, then round each term to 0.01 points using round-half-up; sum rounded terms, then clamp/round S to `[0,100]`. Compute `log2(1+W)` as decimal `ln(1+W)/ln(2)` at that precision; pin the algorithm in the ranking policy version and prohibit intermediate binary floats. Retain raw operands and term results for replay. Pure-function tests define fixed reference values.

### Score policy v1

`S = clamp(P + U + F + E + A + L + C + R, 0, 100)`.

| Term | Definition and proposed v1 defaults |
| --- | --- |
| P, up to 35 | P0=35, P1=22, P2=10, P3=0. Existing P4 also scores 0 until owner reclassification; preserve its label |
| U, up to 25 | Priority-weighted unique transitive blocking dependents plus active T-bg stuck-task links. Let `W` be the sum of P0/P1/P2/P3/P4 weights 4/3/2/1/1 over their union, excluding self and landed/cancelled/archived tasks. `U=min(25, 5*log2(1+W))`. Follow only blocking dependency edges; traverse cycle-safely and deduplicate diamond paths and tasks also linked by T-bg |
| F, up to 12 | Current gated or reviewed evidence for the exact current work SHA=12; otherwise branch exists AND attempts>0 gives 8; otherwise 0. Never add 12+8. Stale gate/review evidence gives no 12 points |
| E, up to 10 | Among open epics sorted by `(rank,id)`, ordinal 0 scores 10, ordinal 1 scores 9, sliding to 0 at ordinal 10; no epic scores 0. Add 2 for an explicitly task-named active owner docket decision (not every member of an epic bundle), then cap this subtotal: `B=clamp(epic_points+docket_points,0,10)`. Apply the signed manual adjustment M defined below **after** that cap: `E=clamp(B+M,0,10)` |
| A, up to 10 | `0.7 * min(14, max(0, elapsed_seconds_since_created/86400))`. Thus the specified 14-day cap is 9.80 points; do not round it up to 10 or reset age on a move/retry |
| L, up to 8 | `8 * p_first_pass[kind,size]` from a pinned harness-derived policy table. Use fixed seed priors below until sufficient harness evidence supports an owner-approved update; no online per-claim learning |
| C, down to -6 | S/M/L base cost 0/-2/-4, multiplied by `(1 + 0.5*q)`, with ledger allowance pressure `q` in `[0,1]`. Thus L at full pressure costs -6 |
| R | High risk=-2, low risk=0. Risk penalty never confers approval |

Seed first-pass probabilities (S / M / L): docs `.90/.85/.75`; investigation or research `.85/.75/.60`; mechanical `.95/.85/.70`; fix `.80/.65/.50`; implementation `.75/.60/.45`; review `.85/.75/.65`; unknown/custom kind `.50/.50/.50`. These are explicit priors, not claimed measurements. Persist table version and sample counts; future tuning replaces it only through the lesson process below.

Allowance pressure uses the latest valid ledger reading per enabled worker seat, chosen by `(observed_at,id)` at or before `as_of`. Normalize the recorded used fraction to `[0,1]`; q is the maximum among those readings. Missing readings, no enabled seats, or any enabled seat reading older than 15 minutes yields q=1 conservatively. Use a stable seat list from policy; disabled seats do not contribute. No task's score depends on whichever seat is queried first. A new valid reading creates a new generation, never alters the old replay inputs.

The plan's **“N tasks wait on this”** counts unique active stuck tasks linked by T-bg, independent of W's priority weights. Each link has evidence, not a lead-supplied free counter. Terminal/resolved links stop counting; duplicate or cyclic links cannot inflate U. Dependency-only unblock counts appear separately. U may put a lower-band systemic fix ahead of ordinary higher-band work, without moving its epic or changing its P label. This is an automatic score boost, not an OP override or an unconditional precedence rule: other terms and override levels still apply.

Golden example: P1, union containing one P1 dependent (W=3), branch+attempts, no epic/docket/manual adjustment, age 10 days, docs/M, q=0, low risk, no override gives `22 + 10 + 8 + 0 + 7 + 6.80 - 2 + 0 = 51.80`. A new P0 task with no dependents/epic, age 0, mechanical/S, q=1, low risk gives `35 + 0 + 0 + 0 + 0 + 7.60 + 0 + 0 = 42.60`. OP3 orders that second task ahead of the first despite its smaller score. Non-power-of-two fixtures include W=2 giving U=7.92 and W=4 giving U=11.61; tests also fix boundary rounding cases.

### Lead reorder, bands, pins, and exact order

The owner-set manual priority band defaults to the task's P label and registered repo; only the owner changes band assignment/rules or grants cross-band queue control. A lead move must name a before/after neighbor in the same band and have a nonempty reason, expected queue revision, and the lead queue capability. The request changes a manual rank and logs the old/new intent. It cannot move the task to another epic, mutate its P label, or edit a dependency to game pressure.

A manual position must influence **a score input**, because the owner-approved comparator has no independent manual-sort key. Proposed v1 synthesis: use a signed manual adjustment M inside E, retaining its `[0,10]` bounds. A positive-only credit disappears when the epic/docket subtotal already reaches 10; applying signed adjustments after capping that subtotal lets a move lower the following task's E even at the ceiling. It also lets a move raise the preceding task's E at the floor. Tasks outside the manual list retain the original epic/docket score.

Within a band, explicitly moved tasks and their enrolled neighbors form a ranked list. Inserting before/after an unlisted neighbor first enrolls it in its current score order, then inserts the moved task at the requested position. After excluding pinned entries, let m be the number of entries and i their zero-based position. For each entry at the move snapshot, compute available headroom `upper=min(2,10-B)` and `lower=-min(2,B)`. For m>=2, `M=upper-(upper-lower)*i/(m-1)`, rounded to 0.01 with the score policy's decimal rules; for m<=1 or an unlisted task, M=0. Thus at B=10 the adjustments span 0 to -2, at B=0 they span +2 to 0, and at B=2 through 8 they span +2 to -2. Interpolate within available headroom before rounding, so three capped entries receive 0/-1/-2 rather than clipping positive adjustments into a tie. Persist each signed adjustment as a ledger score input and recompute all affected adjustments and scores atomically. Between moves, M remains fixed while changes to epic/docket evidence recompute B and E. Apply `E=clamp(clamp(epic_points+docket_points,0,10)+M,0,10)`; do not add M before the inner clamp, where excess docket points could absorb its negative effect.

The UI and API receipt show the requested relation, before/after B/M/E/S, resulting comparator order, and whether the relation was achieved. Larger score differences, rounding in long lists, pins, override levels, or final score clamping may prevent exact adjacency; return that fact explicitly. This bounded E refinement is a proposed policy default reconciling manual moves with R1–R5, not a fourth comparator key.

A pin freezes that task's signed manual adjustment and excludes it from list normalization; it does not freeze its age/U score or guarantee an absolute queue position. Neighbors cannot indirectly alter a pinned adjustment. A forbid rule disallows lead moves for the specified task/band/repo. Owner moves may override these controls explicitly; agents cannot alter owner pins. If the owner wants guaranteed precedence above ordinary work, the lead can set OP3 through the override API with the owner's request as reason, rather than misrepresenting a bounded move as an absolute order. Among several OP3 tasks, S then task ID still decide. Raising a task above another OP3 needs an explicit owner score-input decision or a justified OP1/OP2; there is no hidden manual precedence.

### Ranking generations and policy tuning

Persist one complete ranking generation at a ledger watermark and `as_of`; recompute on relevant task/dependency/unstick/docket/manual/override/policy/allowance events and at a persisted hourly ageing tick. Apply expires-at checks at claim time even between ticks. Claim transaction checks input revisions/generation validity; stale generation is recomputed before selection. UI/API/CLI return the same generation, `as_of`, policy hash, operands, score, and wait reasons. They do not recalculate independently in JavaScript or terminal code.

Weights and tables live in validated config and the versioned policy snapshot. E-v may propose a lesson with evidence and replay old/new policies against saved generations, showing changed first picks, starvation, throughput, first-pass outcomes, and risk distribution. Only an owner action activates a new version after replay and review. No automatic weight tuning, silent config override, or model output writes a live score. Existing runs retain their pinned execution policy; claim ordering uses the current approved ranking policy.

## Override priority and dispatcher behavior

An override is scheduling authority only. Setting one does not approve a task or erase a hold. Require a reason for every set/change/clear, show actor/time/expiry, and clear automatically on landed/cancelled. The owner can set any level from website, docket answer, or `dv op T-x <1|2|3> "reason"`, select OP2 repo/global scope, confirm an agent override, or clear it.

Agents/lead may set OP3 on tasks they can act on without a standing approval grant. They may set OP1/OP2 only with a reason and a durable immediate website inbox/SSE alert committed with the override. Default repo scope for OP2; global must be explicit and prominent. Expiry is no later than four hours after the first escalation for the same unresolved incident; the server binds that incident to the task/escalation chain, so a new request key or switching OP1/OP2 cannot renew it. Agents cannot keep resetting the timer, lower/clear an owner's override, confirm themselves, or replace an owner-confirmed level. Owner confirmation records a new revision and chosen duration (unbounded allowed). OP3 has no automatic four-hour limit unless explicitly set. An expired override is ineffective even if the cleanup job has not run. T-bm exposes confirmation and expiry in the docket; external notifications require their existing owner-enabled adapter.

| Level | Admission and claims |
| --- | --- |
| OP1 emergency | Evaluate before ordinary resource-queue/parallel-cap early returns. At the next dispatcher opportunity, choose the highest ordered **eligible** OP1 and the best eligible seat under current routing/placement policy (stable configured seat order for equal routing outcomes). May raise the global parallel cap by **one total**, not one per OP1. Never cancel/preempt running work |
| OP2 stop the line | Highest eligible permitted OP2 target claims before OP3/ordinary work; overlapping-freeze rules below select the permitted target. Activates a durable new-claim freeze over its repo by default, or global explicitly, from set time until landing, cancellation, expiry, or authorized clear. Running work and all gates/reviews/landing continue |
| OP3 owner first | Ahead of every ordinary task, within ordinary capacity/resource admission; no freeze and no extra place |

OP1 bypasses only the **disk queue hold** (soft disk queue warning) and the global parallel cap up to +1. It does not bypass a hard disk stop/unknown hard-stop reading, other resource holds, physical seat limits, allowance admission, approvals, dependencies, scope conflicts, platform/repo rules, or the owner's dispatch pause. Check/report current disk headroom before heavy work. If the best eligible seat cannot launch safely, use the next eligible seat per routing policy; if none exists, show a precise OP1 wait reason and alert without cancelling work. Persist/adopt the single emergency capacity lease across restart; multiple dispatchers/retries cannot each take +1. Once ordinary capacity becomes free, absorb the emergency worker into it; extra active work never exceeds cap+1 through this mechanism.

An active OP2 freeze blocks *new worker claims*, not completion stages or existing workers. OP1 has precedence through an OP2 freeze and logs that bypass; OP3 cannot cross it. For multiple overlapping OP2 scopes, the highest comparator-ordered active OP2 is the permitted target in the overlap; freezes do not block their own permitted target. Independent repos can each proceed under their own nonoverlapping OP2; a global freeze covers every repo. A blocked/unapproved OP2 still freezes its scope and shows what it waits on; its ordinary prerequisites do not silently become exempt. Use existing running work to finish, a specific OP1 escalation, or owner change/clear to resolve a deadlock. Dependency and resource eligibility still apply to the permitted target. Failed gates, review, or a merely completed worker do not clear OP2: only actual landing or another listed termination does.

The freeze is derived from current ledger overrides at each atomic claim, not an in-memory flag. Set/claim races serialize; claims committed before the freeze remain running work. On restart restore freezes, expire deadlines, reconcile emergency leases with adopted runs, then admit new claims. Keep gates/reviews/landing schedulable during an OP2 freeze even when worker capacity is exhausted; retain their existing resource requirements. Every deferred override displays its blocked reason rather than repeatedly attempting launch.

## API and CLI surfaces

All mutations use the shared action service, authenticated principal, expected target revision/hash, and idempotency key. Route names below are proposed v1 adapters to its registered actions; reconcile T-bm/T-8i/T-8j contracts rather than implementing competing handlers. GETs are read-only projections with permitted-action descriptors. Owner web, owner CLI, and lead API share validation and receipts, with different authority checks.

| Surface under `/api/v1` | CLI / website | Authority |
| --- | --- | --- |
| `GET /docket`, `GET /docket/{id}` | `dv docket [id] [--json]`, Decisions page | Permitted reads; full question, options, text schema, exact bundle/effects/revisions and decision history |
| `POST /docket/{id}/preview` | `dv docket <id> --preview` | Scoped proposer or owner; returns canonical effects/hash, never commits approval |
| `POST /docket/{id}/answer` | `dv docket <id> --answer <option> [--text ...]`, interactive choices/free text | Owner for owner decisions; named grantee only for explicitly grant-eligible approve-only bundle with `grant_id` |
| `GET /tasks/{id}/decisions` and task expansion | `dv show T-x`, task page and worker/reviewer context | Existing read scope; authoritative records and requirement applicability |
| `GET /grants`, `POST /grants`, `POST /grants/{id}/revoke` | `dv grants`, `dv grant create`, `dv grant revoke G-x`; Standing grants page | Owner mutations only; grantee reads its applicable grants/usage |
| `POST /tasks/{id}/grant-approval` | Lead action/API; owner CLI generic action can inspect it | Named grantee, exact grant/revision and task fingerprint; no new owner identity |
| `POST /epics/{id}/approval-preview`, `POST /epics/{id}/approve` | `dv approve-epic E-x --preview`, then explicit manifest submit | Owner submit; proposal-scoped lead preview |
| `POST /queue/reorder` | `dv move T-x --before T-y --reason ...`; plan drag/move | Owner or designated lead capability within owner band; returns actual order/breakdowns |
| `POST /queue/policy` | Plan pin/forbid/band controls; owner generic action | Owner only |
| `GET /tasks/{id}/ranking`, ranking fields on tasks/plan | `dv why T-x [--json]`, plan score expansion | Existing read scope; raw terms, policy/as_of, ordinal, wait counts and eligibility/freeze reasons |
| `POST /tasks/{id}/override`, `POST /tasks/{id}/override/confirm`, `DELETE /tasks/{id}/override` | `dv op T-x <1\|2\|3> "reason" [--scope repo\|global]`, `dv op confirm T-x`, `dv op clear T-x "reason"`; plan controls | Owner any action; agents' bounded rules above. DELETE clears scheduling metadata, never deletes task/history |
| Ranking-policy registered action | Owner policy page / `dv action ...` with replay artifact | Owner only; E-v may propose/replay |

Answer request example (JSON field names are contractual concepts, not a new token format):

```json
{"choice":"yes","text":"Use the scoped PyYAML approach","bundle_version":3,"bundle_hash":"<preview hash>","expected_revision":7,"request_key":"<unique request key>"}
```

Response includes `decision_id`, bundle ID/version, principal/authority, applied task IDs, per-task effects/eligibility, resulting revisions, event ID, and digest/notification reference. A grant-backed request additionally names exact grant ID/revision. Server rejects unknown authority-bearing fields. Return 403 for authority/grant mismatch, 409 for stale revisions/reused key with different payload/changed manifest, 422 for invalid effects/schema/cycles, and a structured `grant-cap-exhausted` refusal with remaining=0. Reads outside principal scope remain forbidden. No partial-success answer response.

CLI pairing remains T-8i's explicit browser owner flow; `dv` is a convenience name for the existing owner CLI surface. It never falls back to a worker bearer for approval. Noninteractive use requires an explicit payload/hash; a missing TTY or Enter is never “approve all.” Interactive mode shows exact effects and final explicit submit, preserves answer drafts on interruption, and returns stale conflicts with the committed decision available. Old task approve/question/reorder routes delegate to the service and cannot bypass classifications, pins, grant checks, or event requirements.

`dv why` and the plan show: override/reason/expiry/confirmation, S and all eight terms, U evidence and “N tasks wait on this,” B/M/E and manual pins, score generation/time, policy version, and “cannot claim because …” when applicable. The plan distinguishes priority band from override level. Score/override JSON is also in list/detail API responses so clients need not scrape HTML.

## Events and the self-improving harness (E-v)

Use append-only `task_events` with stable event IDs, schema version, correlation/request/decision ID, authenticated principal and authority basis, timestamp, target and before/after revisions. Store no token/secret. A single canonical decision event owns the decision; task-linked reference events do not count as separate owner answers.

| Event family | Required evidence / consumer |
| --- | --- |
| `bundle.proposed`, `bundle.superseded`, `decision.applied`, `decision.rejected` | Bundle version/hash, membership, question/answer reference, effects, transaction result; T-bm/T-bh propagate |
| `decision.assessed`, `decision.requirement-mismatch` | T-bl assessment/evidence, decision/task fingerprints; measure compliance and repeat approval blocks |
| `grant.created`, `grant.used`, `grant.denied`, `grant.revoked`, `grant.expired` | Owner/grantee, revision/predicates, use/task count, day/cap, refusal reason; daily digest and E-v |
| `epic.approval-previewed`, `epic.approved` | Exact manifest hash, included/omitted/excluded IDs and all exclusion reasons; no effect from preview |
| `queue.reordered`, `queue.policy-changed` | Actor/reason, band, old/new manual ranks and signed M adjustments, before/after B/E/S, affected IDs, pins, requested versus effective order |
| `unstick.fix-linked`, `unstick.fix-resolved` | T-bg incident/evidence and unique task pair; produces N/W, not a mutable supplied count |
| `ranking.computed`, `ranking.policy-proposed`, `ranking.policy-activated` | Generation/watermark/as_of, saved inputs/policy hash, changed scores, replay artifact and owner decision |
| `override.set`, `override.confirmed`, `override.expired`, `override.cleared`, `override.deferred` | Level/scope, reason, actor, incident/expiry/confirmation, blocked reason; alert and dispatcher audit |
| `dispatch.emergency-admitted`, `dispatch.freeze-applied`, `dispatch.freeze-bypassed` | Claim/run ID, override revision, resource verdict, cap before/after, seat and freeze scope |

Rejected/denied requests are logged after the failed mutation transaction in a separate audit transaction; they contain no approval receipt and cannot leave partial effects. Deduplicate repeated identical defer/denial alerts; retain occurrence counts and timestamps. Daily digest is a website/inbox projection (optional notification adapters stay opt-in). It shows grant uses, outstanding exclusions, overrides/expiry, systemic boosts, and links to each decision and event.

E-v lessons measure owner interactions per unlocked task, decision-to-resume latency, duplicate “approval missing” blocks, direction compliance, grant cap/denial/revoke behavior, exclusion escapes (must be zero), ranking changes and first-pass/finish throughput. A lesson can propose grants or weights; it cannot grant itself authority or activate a policy. Keep saved input generations and decision provenance sufficient to replay a first pick without rereading mutable live state.

## Migration and rollout

1. Append storage/action contracts with feature flags off. Preserve existing `approved_at`, task states, ranks, holds, runs, and owner-answer mappings. Mark preexisting approvals as legacy provenance without fabricating question text, owner identity, or time beyond known evidence.
2. Import today's numbered docket questions into stable item IDs and bundle drafts, preserving question numbers, full options/text, source reference/hash, task mapping, and original answer evidence. Namespace question IDs by docket/round so Q6 or D1 cannot collide across specs. One legacy question maps once; rerunning import cannot duplicate tasks, links, or release effects.
3. The lead prepares the exact mapping of “Q6 — yes” and the PyYAML D1 answer to current task IDs, intended edits, and approval fingerprints. Verified existing authenticated owner records can be linked as historical evidence. A transcript or lead-written 135-task script alone cannot mint authenticated approval: stage it as imported evidence needing owner confirmation of one reviewed bundle. Preserve valid existing task approvals in the meantime; do not ask for 135 individual approvals again.
4. Show the owner unresolved/ambiguous mappings, already-applied edits, and exclusions in one migration preview. Confirmation writes decision links for the exact approved task versions and applies only missing effects. No duplicate dependency edits, cap usage, or state resets for already-applied work. Hold only a mismatched requirement; migration must not blanket-block approved work or claim a missing D1 if matching current evidence exists.
5. Enable bundles and T-bh brief/reviewer propagation first on one docket. Verify crash/idempotency and answer-to-worker visibility, then enable broadly. Turn on grant creation/use and epic manifests only after exclusion classification and cap/revoke tests pass. No grant exists by default; no old epic approval is extended to new tasks.
6. Compute shadow ranking generations from current ledger ranks, dependencies, attempts, allowance readings, and unstick links. Preserve epic ordering as ordinal E input. Retain task ranks as legacy display/order evidence, but start manual adjustment M at zero; do not import every old fractional rank as a covert override. Show the owner old/new first picks and material differences. P4 stays P4 with zero P points; no silent relabel. Existing explicit owner pins/moves are presented for conversion to the new manual controls or OP3, not guessed.
7. Owner activates the approved ranking policy after replay. The new comparator replaces the old one entirely. Do not move a systemic task's epic as an ordering workaround. Enable override dispatcher paths only after cap+1, freeze, restart, and resource tests; no override is inferred from a P0 label, urgent comment, or old rank. Existing runs/gates/reviews keep operating; ranking affects future admissible work.
8. Rollback disables new bundle submissions, grant uses, ranking activation or agent override creation independently. Committed decisions/history remain readable and existing valid approvals remain bound. Ranking rollback restores the saved legacy comparator/policy for future claims with an owner event. Existing overrides/freezes remain enforced until owner-cleared/expired, or override creation is disabled and the owner explicitly reviews/clears them; never silently drop an active stop-the-line. No destructive down-migration.

## Verification and acceptance

Implementation uses existing authorized environments and isolated temporary ledgers/fake principals/clocks/seats/resources. No live publication, credential copying, package install, new service, or main-checkout write is needed. Run each slice's focused tests and the repo's required gate; only Drover may claim a gate receipt for the exact committed SHA. The following are acceptance tests to implement, not passes claimed by this docs task.

| Area | Required assertions |
| --- | --- |
| Bundle transaction | Option and free-text decisions; deps/scope/priority/handoff effects; invalid last member rolls back every prior edit/link/stamp/event; cycle/unknown field rejected; changed fingerprint invalidates preview; crash before/after commit and idempotent replay; two-channel race has one decision |
| Authority | Real owner UI and paired credential succeed; bearer/worker cannot forge owner via actor, headers, status, generic event, answer text, or old endpoints; revoked/expired CLI credentials fail; grants cannot create/widen grants or policies |
| Visibility/compliance | Every affected worker/reviewer sees who/when/question/answer and exact applicability; decision survives brief pressure and resume/relaunch; PyYAML D1 regression no longer blocks on a satisfied requirement; T-bl detects simple violated directions; old decisions cannot authorize changed work |
| Grants | Conjunctive clauses/solo binding/kind/size/risk/effect checks; unknown/composite effects excluded; all never-grantable categories tested individually; 8-task use costs 8; retries cost zero; concurrent final-cap race cannot overshoot; timezone/DST boundary; revoke/use race; expiry and admission recheck; digest shows all uses |
| Epic | Complete exclusion list including multiple reasons; no selecting exclusions via API; optional omissions; added/edited task rejects manifest; one bad member rolls back subset; included work can still wait on excluded prerequisite; no implicit future-task approval |
| Pure scoring | Fixed operands produce exact golden 51.80 and 42.60 scores; fractional log rounding fixtures; term caps and S clamp, P4 and unknown-kind defaults, 14-day ageing=9.80, F nonaddition/current-SHA validation, allowance missing/stale snapshots; same snapshots/as_of/policy give identical scores across replay |
| Graph/queue | Diamond/transitive/cyclic graphs deduplicate; dependency/T-bg overlap counts once; resolved/terminal links lower N/U; ordinary systemic fix can overtake its band; task ID breaks exact ties; signed manual adjustment stays in E, pins survive neighbor moves, forbidden/cross-band moves fail; reason/event and effective-order receipt required |
| Capped-E reorder | Two same-band top-epic tasks reverse order on a requested move using the fixture below; repeat with docket points already exceeding the cap and with E at the floor; before/after B/M/E/S agree across event, API, plan and CLI; pinned adjustment is unchanged by neighbor moves; no extra comparator key |
| OP1 | Best eligible seat; ordinary cap+1 exactly, multiple OP1s cannot stack slots; disk queue warning bypass but hard stop/other holds/dispatch pause retained; no running cancellation; unavailable seat/prerequisite reports wait; restart/adoption retains emergency lease; OP1 crosses OP2 with event |
| OP2 | Repo freeze blocks only matching new claims; global blocks all ordinary/OP3 claims; own permitted target proceeds; overlapping freezes deterministic; blocked target still freezes; set/claim race; running work, gates, reviews, landing continue; failure does not clear; landing/cancel/expiry does; restart restores |
| OP3 and override authority | Above ordinary work with no cap exemption/freeze; owner web/CLI/docket parity; agent OP1/OP2 require reason+durable alert+four-hour expiry; no self-confirm/reset loop/owner-override edit; confirmation persists; expiry effective before sweeper; terminal auto-clear |
| Projection/events | API, plan, `dv why` expose same generation/breakdown and eligibility; correct N label; owner/grant attribution distinct; one canonical decision event, task references, durable digest/alerts; denial audit cannot authorize; HTML/Markdown/text safely rendered |
| Migration/lessons | Old questions/approvals/ranks preserved; Q6/D1 namespace and one-time mapping; unverified script cannot mint owner decision; old valid approval not lost; shadow ranking makes no claims; replay deterministic; weight changes require owner activation; rollback keeps history and explicit override state |

Capped-E reorder fixture: T-a and T-b are eligible, unpinned, unlisted tasks in the same repo/P1 band and top epic, with no override. Both are new docs/S tasks, age 0, low risk, W=0; all terms except P=22, E=10 and L=7.20 are zero. Before the move, both score 39.20 and task ID orders T-a before T-b. Submit `dv move T-b --before T-a --reason "owner requested"`. The manual list becomes `[T-b,T-a]`, producing:

| Task | B | M | E | S | Effective position |
| --- | --- | --- | --- | --- | --- |
| T-b | 10 | 0 | 10 | 39.20 | 1 |
| T-a | 10 | -2 | 8 | 37.20 | 2 |

Assert the unchanged `(effective_override_order,-score,task_id)` comparator now returns T-b before T-a, and the receipt reports the relation achieved. Repeat with both tasks docket-named: epic+docket=12 is first capped to B=10, so the same move still yields E=10/8, never 10/10. Repeat without epic/docket: B=0, the move yields E=2/0 and S=31.20/29.20. Extend the capped fixture with otherwise identical T-c and move it before T-b: `[T-c,T-b,T-a]` gets M=0/-1/-2, E=10/9/8, and S=39.20/38.20/37.20, so all three positions follow the request despite reverse task-ID order. Pin T-a's M=-2 and move another neighbor; assert its M remains -2. Also test a larger non-E score gap where the requested relation is not achieved and the receipt/UI explicitly report that result. These are implementation acceptance fixtures, not runtime tests executed by this docs task.

Acceptance is met when the document and subsequent implementation cover all three layers, exact owner/lead boundaries and forbidden effects, decision evidence in briefs, queue reordering and systemic boost, the prescribed comparator and score terms, OP1–3 behavior, APIs/CLI/plan parity, today's migration, and the tests above. Existing dependencies must be landed interfaces, not merely similarly named task proposals.

## Implementation handoff: small, independently landable tasks

These are proposed implementation slices, not newly created ledger task IDs. Assign real IDs/scopes after reconciling dependency SHAs. Each row lands a single bounded change with focused tests; no slice enables an unfinished feature. Schema/action logic, adapters, UI, and dispatcher are separated so flags can remain off until end-to-end acceptance. A blocking external dependency is explicit even if it is outside this task's checkout.

| Slice | Deliverable and likely file boundary | Dependencies / landing evidence |
| --- | --- | --- |
| B1 | Append bundle/decision/task-link storage and typed effect schemas in ledger modules; immutable evidence and idempotency constraints | T-bm docket identity, T-bh propagation contract; migration/schema tests |
| B2 | Atomic decision action, fingerprint validation, holds/approval linkage, batch receipt; no UI | B1, T-8j batch validation, T-bm shared action service; rollback/race/retry tests |
| B3 | Website docket preview/answer adapter with options, draft free text and effect review | B2, T-bm; UI authority and stale-preview tests; first usable owner bundle path |
| B4 | Paired `dv docket` adapter, parity and explicit noninteractive submit | B2, T-8i/D5, T-bm; token isolation and CLI/web parity tests |
| B5 | Task projection and worker/reviewer/resume decision context, requirement lookup | B2, T-bh; brief-budget/resume tests and PyYAML D1 regression |
| B6 | T-bl direction assessment linkage and deduplicated mismatch docket events | B5, T-bl, T-bm; satisfied/violated/unknown evidence tests |
| B7 | Idempotent legacy-question mapping and reviewed migration preview | B3, B5, T-bm, T-8j; today's Q6/D1/script fixtures; enable bundles first only after B3/B5 coverage |
| G1 | Server effect classifier and never-grantable rules; no grant activation | B1, landed task/action capability metadata; each exclusion/unknown-effect test |
| G2 | Grant storage, owner create/revoke actions and timezone/cap accounting | B2, G1; concurrent cap/revoke/DST tests, flags off |
| G3 | Named-grantee approval action/admission recheck and auditable use events | G2, B5; predicate/old-endpoint bypass tests |
| G4 | Standing-grant owner page and daily-digest projection with one-click revoke | G3, T-bm inbox; web/digest tests; enable grants only after owner creates one |
| E1 | Epic exclusion manifest preview and atomic ordinary-subset approval action | B2, G1, T-8j; changed-membership/exclusion tests |
| E2 | Epic owner page and paired CLI manifest submit adapter | E1, T-8i; exact-subset parity tests |
| Q1 | Pure scorer, validated config/policy snapshot and fixed likelihood priors | Landed harness data contract, no activation; golden/rounding/graph tests |
| Q2 | T-bg fix-link projection and deterministic N/W evidence inputs | Q1, T-bg; diamond/dedup/resolution tests |
| Q3 | Persist ranking generations, allowance/as_of inputs and shadow old/new replay | Q1, Q2, E-v snapshot contract; deterministic replay and P4/rank migration tests |
| Q4 | Owner band/pin/forbid policy and scoped lead reorder action with signed E adjustment | Q3, T-bm action registry; capped-E/floor move fixtures, bounds/pin/race/event tests |
| Q5 | API ranking projection and `dv why` | Q3, T-8i CLI transport; exact-breakdown parity tests |
| Q6 | Plan score, wait count, move/pin controls and effective-order display | Q4, Q5; UI projection and misleading-position regression tests |
| Q7 | Switch `ready_tasks` comparator with freshness recheck behind owner activation | Q3, Q4, Q5, Q6; dispatch order tests and reviewed replay |
| O1 | Override storage/actions, expiry/confirmation rules and durable owner alerts | B2, T-bm, T-8i; agent/owner/race/expiry tests; dispatcher flag off |
| O2 | OP3 ordering and API/plan/CLI controls through override action service | O1, Q7; ordinary-cap and ordering parity tests |
| O3 | OP2 repo/global freeze and restart reconciliation | O2; scope/overlap/blocked-target and completion-progress tests |
| O4 | OP1 early dispatch path, one emergency lease and disk-queue exception | O3, existing admission/seat/resource contracts; cap+1/hard-stop/restart tests |
| H1 | E-v event ingestion, digest metrics and owner-reviewed weight lesson/replay action | B6, G4, Q3, O4, E-v; attribution/replay/owner-activation tests |
| R1 | Flagged rollout/migration/rollback checks and plain-language in-product help from this owner page | B7, E2, G4, Q6, O4, H1; focused end-to-end fixtures plus required repository gate |

Land B1–B7 before enabling later approval layers. Q/O slices may develop independently but cannot replace approval propagation or turn their flags on prematurely. Every implementation task names its exact allowed files, dependencies, acceptance fixtures, and gate; workers leave ready_to_commit for Drover to commit and gate. No script executes owner approval on the lead's behalf, and no part of this handoff grants permission to push, merge, publish, install, or modify main directly.
