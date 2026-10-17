# Configurable task types, lifecycles and owner work

Date: 2026-10-17. Status: proposed; recommendations below enable planning, not deployment approval.

## Problem, intent and boundaries

Owner direction: ship useful YAML task types that owners can extend, with fields, access, routing, context, lifecycle and delivery policy. Give owners equal web/CLI capabilities, make grilling a durable agent/owner loop, and respect each repository's spec framework. T-7 must become the first consumer. Success means one resolved contract drives validation, execution and both presentations; editing configuration never changes a running task's meaning.

This document specifies contracts and a staged implementation; it changes no product code. Non-goals: a general workflow programming language, arbitrary YAML hooks, vendoring skills, installing frameworks, remote publishing, GitHub team/PR integration (T-b consumes these contracts), or redesigning admission. T-8 can consume the same actions without coupling this work to its delivery.

## Evidence on main and T-7

All current-behavior claims below use this inventory. `main:` means commit `acd5b6b2853b39d08fc5a3e5e365c2f107843123` (also this task's base). `T7:` means the read-only, uncommitted T-7 working snapshot inspected on 2026-10-17, based on that commit; paths are relative to that worktree, not main. T-7 may move before implementation: reconcile symbols, never copy its migration number blindly.

| ID | Grounding and current behavior |
| --- | --- |
| E1 | `main:drover/ledger/schema.py:7-30,46-58,61-73`: fixed task columns, JSON text acceptance/scope, runs with policy versions, questions, policy snapshots, migrations through v3. `main:drover/ledger/ops.py:22-24,189-229`: fixed editable fields and create/patch operations. |
| E2 | `main:hub/api.py:26-54,213-257`: fixed TaskIn/TaskPatch schemas and store delegation. `main:drover/importer.py:179-189,220-260`: fixed field projection, imported tasks remain proposed, owner edits produce conflicts. |
| E3 | `main:drover/ledger/states.py:3-24,31-37`: twelve statuses, fixed transition graph; only proposed/draft → ready explicitly requires owner. `main:drover/ledger/ops.py:286-329,399-463`: approval stamp, append event on transition, ready selection requires blocking predecessors landed, active scopes and atomic claim. |
| E4 | `main:drover/ledger/schema.py:32-40`: append-only task events. `main:drover/ledger/ops.py:685-734`: questions with options/free text, last answer requeues an approved needs-owner task. `main:hub/api.py:310-325`: /questions answer is owner-only. |
| E5 | `main:drover/brief.py:7-30,49-97,100-119`: size-based brief/review budgets, fixed task block and action records, trimming. `main:drover/launch.py:154-160,253-254,378-388`: context rotation/handoff and model ceiling lookup. `main:drover/pinned.md:5-31`: scope, landing queue, exact-SHA receipts, no compaction and worker constraints. |
| E6 | `main:drover/config.py:16-23,38-75,389-408,465-495,547-558`: stdlib imports including tomllib, seats/model ladders, config layers and effort resolution. `main:drover.example.toml:33-88`: seats, reserves, sandbox, models, review, ladders. `main:drover/route.py:28-62,187-209,229-279`: role/size routing, seat/platform and allowance filtering. |
| E7 | `main:drover/dispatcher.py:137-157,740-753,1123-1149,1243-1277`: placement, reviewer routing, escalation, claim → known gate → worktree → launch. `main:drover/pipeline.py:68-93,186-217,347-366`: scoped Codex commits, isolated worktrees, gate selection none/scoped/full. |
| E8 | `main:drover/landing.py:1-7,116-138,521-543`: batch tip gating, exact reviewed SHA, fast-forward landing and ledger update. `main:drover/repos.py:41-103`, `main:repos/acme-app.toml:5-21`: repo run/gate/review/land settings. |
| E9 | `main:hub/auth.py:156-184,190-221`: trusted UI identity/Host/port/CSRF checks, bearer → token principal; documents mirrored-WSL loopback limitation. `main:hub/api.py:161-172,260-263,321-325`: UI identity is owner; bearer is not. `main:hub/app.py:59-75,84-85`: API/web auth and router wiring. |
| E10 | `main:hub/web.py:1-21,62-65,181-188`, `main:hub/store.py:188-210,394-405,564-565`: server-rendered inbox and answer adapters. `main:drover/__main__.py:1-17`: top-level CLI currently only init/config. |
| E11 | `main:drover/tuner.py:211-227,410-420,472-505`, `main:drover/ledger/ops.py:112-122,491-504`: route statistics/leaders, policy snapshots and run policy version. `main:hub/pyproject.toml:5-16`, `main:README.md:19-27`: hub dependencies and separate Windows/WSL Python environments. |
| E12 | `T7:drover/task_context.py:6-13,16-44`, `T7:drover/ledger/schema.py:73-85`, `T7:drover/ledger/ops.py:192-247`: seven fixed kinds, read-only defaults, write/read_access, task_links and validation. `T7:drover/task_access.py:49-98`: immutable read snapshots and verification. |
| E13 | `T7:drover/brief.py:59-68`, `T7:hub/task_views.py:51-55,130-172`, `T7:hub/templates/_task_card.html:1-16`, `T7:hub/templates/task.html:14-44`, `T7:hub/api.py:37-68`: type/access brief, safe Markdown, cached repo state, fixed card/page/edit controls and API fields. |
| E14 | `main:drover/ledger/db.py:72-88`: forward migrations each run transactionally. `main:drover/ledger/board.py:5,25-41`: board groups fixed statuses and counts landed events. |
| E15 | `main:drover/port.py:1-4,44-70,135-147`: dispatcher uses DirectPort; HttpPort is an unwired compatibility stub. `main:hub/dispatch.py:24-53,146-157`: HubPort wraps the dispatcher operations and LandingService selects literal reviewed status. |

## Alternatives and lessons

1. **Recommended: declarative registry plus constrained execution kernel.** YAML resolves to immutable JSON; types describe fields/forms and a lifecycle; core services own permission, leases, receipts and landing. More initial integration than templates, but a single durable contract.
2. Templates alone: inexpensive field/default generation, but cannot meet owner task, lifecycle and replay requirements; routing and completion stay divergent.
3. Arbitrary workflow engine: maximum flexibility, substantial security/recovery surface and new execution machinery. Unnecessary for the requested types.

[GitHub issue forms](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema) inform stable field IDs, widget hints and required validation; Drover additionally validates server-side against a saved version. [GitLab template inheritance](https://docs.gitlab.com/ci/yaml/yaml_optimization/) informs recursive map merging with whole-array replacement; use one parent to avoid ordering puzzles. Neither precedent substitutes for Drover's execution permissions.

## Registry, layering and format

Shipped definitions: `drover/task_types/*.yaml`, lifecycles in `drover/lifecycles/*.yaml`, brief templates in `drover/task_templates/*.md`. Owner definitions: `<DROVER_HOME>/task-types/` (normally `~/.drover/task-types/`). Optional repo-specific layer: `<DROVER_HOME>/repos/<repo>.task-types/`, enabled by `[task_types] directory = "<repo>.task-types"` in the registered `repos/<repo>.toml`; relative paths resolve beside that file. Nothing loads configuration from a worker checkout. This follows the trusted-config distinction in E6, with an explicit new repo layer.

Precedence: shipped < owner < enabled repo layer. Each document has `schema_version: 1`, stable `name` (`[a-z][a-z0-9-]{0,63}`), human `revision`, description, and optional single `extends`. IDs are case-sensitive. Duplicates within a layer fail. `extends: builtin/spec` pins a shipped parent; unqualified parents use the final registry, excluding the child. A same-name override must explicitly extend `lower/<name>` (nearest lower layer) or declare `replace: true`; no implicit self-inheritance. Reject cycles, missing parents, depth >8 and unknown keys. Parent maps merge recursively; scalars and arrays replace; `remove_fields: [id]` explicitly removes custom fields, null is never a deletion. Core fields cannot be removed. Source diagnostics name file, line and schema path.

Owner reload is validate → preview effective diff → activate registry revision atomically. Broken edits leave the previous revision serving; first boot with no valid revision refuses dispatch. Source layers, parent hashes, framework mapping and lifecycle content contribute to canonical sorted JSON SHA-256 `type_version`; comments and YAML formatting do not. Cache revisions in the ledger; runs never reread live files. Reload and type upgrades are owner actions, not worker settings changes.

**Format decision D1:** recommend PyYAML, isolated behind `decode_yaml(text) -> dict` in a hub-loaded codec. [PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation) provides a safe loader and optional LibYAML acceleration; use a constrained SafeLoader, never arbitrary constructors. Cost: one maintained third-party dependency, a pinned hub lock entry, clean-environment checks and Windows/WSL packaging tests. Putting YAML parsing into every drover process would require installation in both Windows and WSL and end the stdlib-only worker contract (E6/E11). Instead, the Windows hub parses; WSL workers and owner CLI consume normalized JSON over API and remain stdlib-only. A standalone YAML validation command must run in the hub environment or call its validation endpoint, with a clear missing-parser error.

Alternative: TOML via [stdlib tomllib](https://docs.python.org/3/library/tomllib.html) matches E6 and adds no dependency, but contradicts the owner's YAML direction. A strict handwritten YAML subset avoids a package but creates a parser maintenance/security burden and surprising incompatibilities with normal YAML; reject. The schema, registry merge, hashing and field validators accept JSON-compatible dictionaries independently of the codec, so an owner can later approve a TOML frontend without changing saved types. No dependency is installed by this spec task; D1 approval is required before dependency implementation.

Codec limits: UTF-8, one mapping document, ≤256 KiB, nesting ≤32, ≤1,000 field/state definitions combined; string keys, JSON-compatible finite values only; booleans only `true`/`false`, no implicit date objects. Reject duplicate keys, tags, anchors, aliases and merge keys; JSON-like scalar resolution avoids YAML's `yes`/`on` surprises. These restrictions are validation atop a real parser, not a second YAML parser.

## Type contract and a worked extension

Core fields stay typed columns: identity, title/goal (description remains alias), repo, acceptance, scope_files, priority/size/risk, deps, related links, write/read_access, branch/worktree, approval and run metadata (E1/E12). `kind` becomes the API compatibility alias for `type`; conflicting values are rejected. Type-specific values live in `fields` JSON, never collide with core names. Supported field kinds: string, markdown, integer, number, boolean, enum, list (typed items), repo-ref, task-ref, uri; required/default/nullable, numeric and length bounds, enum values and ordered card/page visibility are declarative. Unknown fields, bool-as-int, nonfinite numbers and invalid refs fail validation; no coercion from strings except documented web/CLI transport decoding. Defaults are materialized once at creation, including explicit false/empty values; patches validate the merged record against its pinned version without refilling deleted values. Required means present and non-null unless nullable; required strings must be nonempty after trimming.

Type sections: `assignee`, `fields`, `access`, `workspace`, `routing` (worker/planner/reviewer), `context`, `skills`, `acceptance`, `gate`, `review`, `landing`, `followups`, `lifecycle`, `forms`, and a routing-only `tunable` allowlist. All sections below are supported; omitted sections inherit or take schema defaults. The shipped `spec` parent is fully resolved by the defaults table and rules in this document. Here is an owner extension suitable for a repo using superpowers:

```yaml
schema_version: 1
name: api-spec
revision: 1
description: Design an API contract, resolve its decisions, and propose implementation.
extends: builtin/spec
assignee: agent
fields:
  target:
    type: string
    required: true
    min_length: 1
    max_length: 200
    shown: [card, page]
  compatibility:
    type: enum
    required: true
    default: additive
    enum: [additive, breaking]
    shown: [card, page]
  evidence:
    type: list
    items: {type: uri}
    required: false
    default: []
    shown: [page]
access:
  write: true
  read_access: ["${repo}@${default_branch}"]
  sandbox: workspace-write
workspace: {worktree: new, branch: new}
routing:
  worker: {backend: claude, model: "${models.director}", effort: high,
           escalation: ["${models.director}"]}
  planner: {backend: claude, model: "${models.director}", effort: high,
            escalation: ["${models.director}"]}
  reviewer: {backend: claude, model: "${models.review}", effort: high,
             escalation: ["${models.review}", "${models.director}"]}
context:
  template: builtin/spec.md
  include: [AGENTS.md, GLOSSARY.md, "docs/adr/*.md"]
  framework: respect
  budget_tokens: 15000
  handoff_at_tokens: 60000
  compact_at_tokens: null
skills: [{name: brainstorming, required: true}, {name: writing-plans, required: true}]
acceptance:
  defaults: ["Document compatibility and cite source evidence.", "Record owner decisions and recommendations."]
gate: scoped
review: {required: true, role: reviewer}
landing: {mode: spec-only, artifact: "${framework.design}", plan: "${framework.plan}"}
followups:
  allowed: [grill, implementation, docs]
  propose_requires: [grill-complete]
lifecycle: standard-v1
forms:
  create:
    fields: [title, repo, goal, target, compatibility, evidence]
    web: {layout: stacked}
    cli: {mode: sequential, order: [title, repo, goal, target, compatibility, evidence]}
  answer:
    fields: []
    web: {layout: stacked}
    cli: {mode: sequential, order: []}
```

Creating `{type:"api-spec", repo:"acme-app", fields:{target:"orders"}}` materializes compatibility=`additive`, evidence=`[]`, the read ref, acceptance defaults and spec-only delivery. It starts proposed, pins the resolved hash and displays target/compatibility on card and all three on page. Owner approval creates the approved execution revision. The agent writes only approved framework artifact paths; it can propose a grill. Until the grill is complete, proposing implementation returns a prerequisite error. Reloading an extension with a different model leaves this task unchanged; an owner can explicitly upgrade it while proposed/draft and review the diff.

Templates use a small placeholder allowlist (`repo`, `default_branch`, task identity, core/custom values, framework outputs, registered model paths); unknown placeholders fail. No expressions, shell expansion, executable templates or arbitrary includes. Custom values render as escaped text. Resolve task-dependent paths after ID creation within the creation transaction; enforce normalized, repo-relative artifact/scope paths and reject traversal, symlink escape and absolute paths.

## Access, delivery, routing and context

Access is an owner-approved capability ceiling: effective write/sandbox/read set = requested policy constrained by task approval, registered repo capabilities, seat/harness capabilities and pinned constraints. Task overrides may narrow; broadening any capability or changing workspace/delivery/routing constraints invalidates approval and requires owner reapproval. A worker may propose such a change, never grant itself permission. Read refs use T-7's `repo[@branch]` syntax and immutable launch SHA snapshots (E12). Unavailable refs/skills become needs-owner, never silently read another checkout. Repo contents may inform context, never authorize policy changes.

Workspace `worktree: new|existing|none`, `branch: new|existing|none`: new creates Drover-owned isolation; existing requires an explicit branch/worktree, ownership or clean-and-explicit-owner adoption, and never the main checkout; none creates no task worktree. Write=true requires new/existing plus nonempty scope. No-repo work uses hub-owned run artifacts; write=false means no repository writes, even when submitting an artifact through the API. Research requesting versioned documentation uses a spec-derived type or a coherent owner-approved override of write=true, workspace=new, delivery=spec-only, gate=scoped and review=required; changing write alone cannot make a no-repo-change type commit. Keep T-7's access and worktree helpers (E7/E12), replacing kind tests with resolved capabilities.

Landing `commit+land` delivers code/docs via scoped commit, gate, independent review and landing queue; `spec-only` uses the same path restricted to framework artifacts and configured glossary/ADR paths; `no-repo-change` stores a result artifact and never enters the landing queue. `artifact` is a framework-relative repo path for writes or `hub:tasks/${task.id}/result.md` for no-repo-change; the hub persists content/hash outside worker write permissions. Gate levels none < scoped < full combine by maximum of type request, explicit repo minimum and high-risk floor. Scoped falls back to full if scope cannot be proven; none is permitted only for no-repo-change or an explicitly approved docs-only exemption with an exact-SHA no-code receipt. Code changes always get scoped/full and independent review; repo writes never bypass exact-SHA evidence (E7/E8). No-repo checks issue artifact-hash receipts instead of invented gate passes. High risk forces full for repo changes and top-tier independent review; owner decisions require no model or code gate.

Routing precedence for defaults: global role/size base → resolved type → owner-approved policy overlay for that type/version/role → explicit task override. Explicit repo reviewer settings override type reviewer defaults; explicit task reviewer overrides follow that, within repo minima. Preserve whether repo keys were explicit rather than letting normalized fallback values mask types (E8). `model: auto` delegates to role/size configuration; registered model placeholders select configured aliases, not claims about external model capabilities. Each role specifies backend (`auto|claude|codex`), model, effort and ordered escalation ladder. Validate backend/model/effort compatibility; escalation cannot leave the approved candidate set and requires the same admission checks on every attempt.

Seats/allowance are constraints, never overwritten by a type: intersect requested backend/model/platform with compatible available seats, reserves, quota and resource admission (E6/E7). Temporary scarcity defers; no compatible route goes to needs-owner with diagnostics. Keep Windows dotnet/MSBuild on Claude Windows and Codex placement restrictions in E5/E6. Do not silently substitute a cheaper model for a pinned model. Planner routing preserves the director-seat rule; a spec worker using the director model still has the worker role and ordinary worker admission.

Context defaults: worker budget S=6,000, M=15,000; reviewer S=12,000, M=30,000 (E5). Type may set an integer budget; require room for pinned constraints, core brief, acceptance, current owner decisions and handoff, then trim optional excerpts deterministically with an omission manifest. Read includes only from approved snapshots/own worktree; bound glob count/bytes to budget, report missing optional files and block on missing explicitly required ones. No other repo is opened because an include happens to match a path. Handoff threshold defaults to min(60,000 tokens, harness/model ceiling when reported); earlier per-type thresholds allowed. `compact_at_tokens: null` is mandatory while E5's no-compact rule applies; non-null is rejected, not an override. At threshold persist handoff ≤1,500 characters plus structured artifact pointers, release the run and resume in a fresh run with the same type snapshot.

`skills: [{name, required}]` resolves against the selected seat's installed skill catalog before launching; record name and content hash in run metadata and explicitly tell the worker to read/invoke each skill in the brief. Required missing → needs-owner with install guidance; optional missing → recorded omission. Drover neither vendors nor installs skills. Examples: review → code-review, fix → diagnosing-bugs (tdd opt-in), PR response → respond-to-pr-review/pr-docket via future extension, grill → one of the installed grilling variants.

## Shipped types and reasons

All rows are new recommended defaults, not descriptions of today's behavior. `balanced` means backend auto, model auto from configured size ladder; `deep` means Claude `${models.director}`/high; `cheap` means auto backend, first configured rung/xhigh. All ordinary reviewers use `${models.review}`/high with director/top candidate escalation; high risk strengthens as above. Normal worker escalation uses the remaining approved configured ladder rungs, one rung after every second failure, preserving E6/E7's policy. Deep starts at top and asks owner when exhausted.

| Type | Fields / purpose | Model, effort and reason | Access / workspace / delivery | Gate / review and reason |
| --- | --- | --- | --- | --- |
| research | question required, sources list; compare evidence | balanced/high: synthesis with bounded scope | read-only / none / hub result | none; no independent review by default, owner accepts evidence artifact |
| investigation | symptom required, reproduction optional; diagnose cause | balanced/high: uncertain causal reasoning | read-only / none / hub result | none; owner accepts cause/evidence; write reproduction/fix is a separate proposed fix |
| spec | problem required, decisions list; design and plan | deep/high: decisions affect downstream work | write / new / spec-only | scoped; independent review of completeness and framework artifact checks |
| implementation | design_ref required; deliver behavior | balanced/high: configured size-cost tradeoff | write / new / commit+land | scoped; independent review because behavior changes |
| fix | symptom required, regression_ref optional; repair behavior | balanced/high: diagnosis before edits | write / new / commit+land | scoped; independent review plus regression evidence |
| review | target_ref required, focus optional; report verdict | reviewer model/high: independent assessment | read-only / none / hub result | none; no review-of-review loop; artifact pins target SHA and findings |
| docs | audience required, artifact optional; maintain explanations | balanced/high: accuracy over cheap rewriting | write / new / commit+land | scoped; independent review for factual correctness, docs exemption owner-configurable |
| mechanical | transformation required; bounded repeatable edits | cheap/xhigh: explicit rules reduce reasoning cost | write / new / commit+land | full; independent balanced review catches broad accidental changes |
| grill | target_task or target_artifact, skill variant, frontier | deep/high: consequential decision probing | read-only / none / hub result; glossary/ADR follow-up docs tasks write | none for rounds; docs follow-ups scoped and independently reviewed |
| owner-answer | questions/answer_schema, parent context; decision, approval or PR reply | no agent/model/effort | owner / none / no-repo-change | none; owner submission is authoritative and audited |

Read-only agent types use sandbox=read-only; write-enabled types use sandbox=workspace-write, constrained to their approved scope and pinned rules. Owner-answer starts no worker sandbox. Every agent type defaults read_access to its registered repo's default branch; owner-answer reads only task context and supplied links. Every type uses standard-v1 unless stated: research/investigation/review/grill use artifact-v1; owner-answer uses owner-v1. Acceptance defaults require cited evidence for research/investigation, framework design/plan/task list for spec, acceptance + test evidence for implementation/fix, SHA findings for review, accurate links/content for docs, transformation/diff accounting for mechanical, and resolved frontier plus linked decisions for grill. Type defaults append to task acceptance with stable deduplication, never erase explicit criteria.

Allowed follow-ups: research → investigation/spec/grill; investigation → fix/spec/grill; spec → grill/implementation/docs; implementation/fix → fix/docs/review/grill; review → fix/docs/owner-answer; docs/mechanical → docs/fix; grill → owner-answer/docs/spec; owner-answer → none. All agent types may request owner-answer operationally. Follow-ups are proposed with parent links/provenance, never auto-approved. `propose_requires` is checked at creation of a follow-up, not just dispatch; a spec can require grill-complete before implementation tasks even appear. T-b may extend allowed task types without core enum edits.

## Lifecycle contract and execution invariants

Lifecycle documents share registry layering/hashing rules. Each defines named states with `phase`, initial state, success/cancel/failure outcome, transitions keyed by stable ID (`from`, `to`, `actors`, declarative guards), and ordered entry/exit actions. Actors are authenticated owner, assigned agent, dispatcher, or registered external-event adapter; payload `actor` strings cannot confer authority. Agents request transitions; dispatcher services validate receipts and apply execution transitions. External adapters verify their own source/event IDs and may satisfy evidence, never approve owner work or fabricate gates.

standard-v1 copies every status and edge from E3: proposed → draft/ready/blocked/cancelled; draft → proposed/ready/blocked/cancelled; ready → draft/claimed/blocked/needs-owner/cancelled; claimed → ready/running/blocked/needs-owner/failed/cancelled; running → ready/gated/blocked/needs-owner/failed/cancelled; gated → ready/running/reviewed/blocked/needs-owner/failed/cancelled; reviewed → ready/running/landed/blocked/needs-owner/failed/cancelled; blocked → proposed/draft/ready/needs-owner/failed/cancelled; failed → ready/needs-owner/cancelled; needs-owner → draft/ready/failed/cancelled; landed/cancelled have no outgoing edges. Preserve owner approval/demotion rules; tighten actor authorization to the capability rules below rather than E3's broad nonapproval edges.

States map to fixed internal phases: proposed/draft=`pending`, ready=`eligible`, claimed=`claimed`, running=`running`, gated=`gated`, reviewed=`reviewed`, landed=`completed`, blocked=`blocked`, failed=`failed`, needs-owner=`waiting-owner`, cancelled=`cancelled`. New names can share a phase. Importable lifecycles must retain a `proposed` pending entry state (draft optional), preserving the explicit backlog approval boundary; other state names and domain transitions may be replaced. Store both `tasks.status` (type state) and indexed `execution_phase`; phase is derived server-side, never client writable. A lifecycle extends/overrides a lower lifecycle or replaces it explicitly; removed referenced states/transitions fail validation. Existing task states remain in their saved lifecycle even if a definition is deleted.

Types may replace standard-v1. artifact-v1 is proposed/draft → ready (owner approval) → claimed → running → completed; running → needs-owner → ready via validated owner rounds and dispatcher resume, then a fresh atomic claim/start; blocked/failed/cancelled escape/retry rules follow standard phases. Completion requires a durable result hash and acceptance record, not a SHA. owner-v1 has proposed (pending), open (waiting-owner), answered (completed) and cancelled states; proposed → open activates a request, owner submit open → answered completes it, and owner may cancel proposed/open. No claim/run phases. Creation starts proposed; the trusted create-owner-task action can activate it in the same transaction under an approved parent's permission to ask questions, with provenance and notification. Independent imported owner work stays proposed until owner activation. These lifecycles are explicit shipped YAML, not magic type-name branches.

Kernel invariants apply across all graphs:

- Only owner actions set approval; entering eligible requires current approval. Entering any pending phase clears it, including demotion to proposed/draft. Agent claim is atomic, assignee=agent, dependencies satisfied and no conflicting reserved write scope. Owner tasks never consume worker seats.
- Only dispatcher owns claim/start/finish, gate and review phase transitions; owner may cancel/retry/hold, but cannot forge execution evidence via a state patch. No repo change reaches completed without queue-recorded landing of the exact gated/reviewed revision. Required stages cannot be skipped, even by a replacement graph or external event.
- Gate none/review optional create explicit policy exemption evidence; never counterfeit successful command/reviewer receipts. Repo baseline minimum and code-change review floor win over YAML. For review-required no-repo types, review an artifact hash, not a fictitious commit.
- Dependency readiness uses successful completion (`execution_phase=completed`, outcome=success), including owner answers/artifacts/landings. Cancelled/failed predecessors never unblock by accident. Preserve blocks versus advisory waits-for and cycle prevention (E1/E3); expose the changed success predicate to all consumers.
- Reserve write scopes from claim through successful landing/cancel-safe cleanup; waiting-owner with unlanded writes retains a reservation even though a worker lease/seat is released. Resume checks ownership, snapshot, session and worktree state. Scope reservations are independent of visible state names.
- Per-state actors/guards can narrow these rules, not weaken them. Reject graphs without required phase reachability and terminal exits, invalid actor/action combinations or mandatory-stage bypass paths.

Action vocabulary: create-worktree, resolve-reads, launch-worker, run-gate, request-review, enqueue-landing, persist-artifact, create-owner-task, notify-owner, resume-worker, propose-followups. No shell/Python hooks; repo gate commands remain separately approved repo config (E8). On transition, atomically validate expected task revision/actor/guards, update state and append event plus durable action-outbox rows. Dispatch actions after commit with `(transition_event_id, action_index)` idempotency keys; persist intent/result and recover incomplete actions. Crash after launching checks run nonce/PID before retry; crash during an unprovable external effect goes needs-owner. Exit actions precede entry actions, ordered within the outbox; phase advancement waits for required action evidence. Audit records state/phase before/after, actor identity, request key, type/lifecycle/policy versions and evidence hashes; retain E4's append-only history.

## Storage, validation and T-7 migration

Add `type_versions(hash PK, name, revision, schema_version, resolved_json, provenance_json, created)`; resolved JSON embeds lifecycle, framework contract and template hashes/content so deletion cannot break replay. Tasks add `type`, `type_version` FK, `fields_json` default `{}`, `assignee` (`agent|owner`), `execution_phase`, `revision` integer, `parent_id`, `result_json` and optional framework binding. Retain `kind` as a temporary compatibility column/alias with equality enforced by operations; remove its fixed CHECK using an audited tasks-table rebuild. Keep core write/read_access and task_links, preserving T-7 clients. JSON column beats EAV for small bounded typed values and snapshot validation; add JSON indexes later only for demonstrated queries.

Runs add `type_version` and `execution_json` containing resolved route/seat, policy version, context/template/skill/read-SHA manifest and effective capabilities; owner replies have action events, not fake model runs. Add scope reservations, action-outbox, owner-task/question mappings and grill-session/round records. Type snapshots are immutable insert-only; policies overlay routing without mutating a type hash. Pin tasks on create; a run uses the task's pinned version, and snapshots policy/effective configuration atomically before launch. Retries/handoffs keep type; new runs may adopt a newer allowed routing policy and record it. Resuming an existing backend session keeps its original execution snapshot or starts a fresh session.

All create/patch/import routes call one pure `validate_task(payload, resolved_type, existing=None)` followed by authorized ledger operations; no API-only enforcement. Type/version fields cannot be patched mid-run. Explicit owner upgrade while pending validates all fields, previews defaults/capability diff, records previous version and invalidates approval. Patch uses expected revision; omitted differs from null; field errors include stable paths. Import validates the whole batch before commit, rejects unknown type/version instead of dropping fields, preserves source identifiers/owner-edit conflicts and proposed status (E2). API 422=invalid values, 409=revision/type-version/prerequisite conflict, 403=unauthorized.

Migration rollout: first land/reconcile T-7; build registry in shadow comparison mode against its defaults; append the next unused migration (never edit v1–v4 if applied; E12/E14). Back up via owner rollout procedure, transactionally rebuild only tasks as needed while preserving IDs, references, links, questions, events, indexes, triggers and sequence settings; run FK/integrity checks. Maintain legacy kind→same type; pre-T7 tasks become implementation with their existing permissions. Preserve each task's explicit T-7 overrides in its legacy snapshot, including write-enabled research, rather than applying new read-only defaults retroactively. Do not infer research from titles. Backfill type snapshots for historical runs using `legacy-v1` compatibility metadata, preserving policy/status/receipt data; mark unavailable original configuration explicitly rather than claiming historical precision. Already-running tasks finish under legacy execution; enable configurable dispatch only after those leases settle. New runs then require a snapshot. Rollback disables new creation/dispatch and restores old binary only from backup; no destructive down-migration against newer states.

## Owner tasks, shared actions, web and interactive CLI

Replace questions as the authoritative workflow with assignee=owner tasks. Retain the questions table and `/questions` API as a compatibility facade: unique mapping question ID → owner-answer task; one historical question → one migrated task, preserving original targets/answers/options/timestamps and deduplicating mappings on retry. New grill rounds are one owner task containing several numbered questions, not one task per question. Legacy question writes/answers route through the same owner action transaction, keeping old readers synchronized. Before migration, snapshot outstanding question→parent dependencies and approval state; do not duplicate requeue effects from E4.

`perform_action(principal, target, action, payload, expected_revision, request_key)` is the single service for approve, reject/cancel, answer, comment, edit, hold/release, reorder, type upgrade/reload and grill start/resume. API and web adapters call it; CLI calls API only, never opens SQLite. Its new stdlib HTTP transport is independent of E15's unwired HttpPort stub; the hub dispatcher remains on DirectPort. Principal is structured and authenticated, not `actor="owner"` from input. Target is `{kind, id}` (task, epic, hub, repo or garden); task routes supply task identity, and global controls use their controller revision/hash for CAS. Existing broader owner controls (dispatch, epic/import approvals, garden, policy decisions; E9/E10) delegate through the same action registry. Both channels receive the same validation errors, receipts and permitted-action descriptors. Action-specific payload schemas are core contracts; a YAML form cannot redefine an approval as a harmless answer.

Owner-answer subforms cover decisions, approval and PR response text with parent context and recommended choices. Creating one writes a notification outbox/inbox event and adds a blocking dependency from requester to owner task; parent_id/related links are navigation only, never reverse dependencies. Answer validates pinned question IDs/options/required free text, persists full answer with actor/time, completes owner task and removes only that owner's hold. Resume only when all blocking tasks/holds clear and parent approval is still current. A comment alone cannot satisfy structured owner work. Cancellation leaves requester blocked with reason until owner resolves/replaces the requirement. Notify via durable website inbox/SSE and CLI inbox polling; optional external notifications are owner-enabled adapters, not a new mandatory service.

Types expose create/edit/answer field lists, web widget/layout hints and CLI order/mode against one field/answer schema. Web widgets: text, textarea, select, checkbox, repeatable list; CLI: sequential questions with choice/default, multiline editor optional, list entry. Missing hints derive from field kinds; unsupported hints reject configuration, never hide required fields. Answer fields may use the bounded `questions` structure (id, number, prompt, rationale, options with stable IDs/recommended ID, required flag, free-text allowance). These are data, not HTML. API returns type version, resolved schema, values, rendered card/page descriptors and permitted actions; web and terminal renderers use these. Keep T-7 safe Markdown, repo-state cache, dependency/related navigation, core chips and event/run pages (E13), replacing hard-coded kind menus/field loops with descriptors. Task creation first selects a type and loads its pinned form; validation failures preserve input.

CLI: `drover login`, `drover inbox`, `drover show T-n [--card|--page|--json]`, `drover create --type ...`, `drover edit`, `drover answer T-n`, `drover approve T-n`, `drover action ...`, `drover grill [T-n|backlog]`. Generic action invocation covers all owner controls with the same schemas. `grill backlog` offers prioritized unresolved rounds and can propose grills for selected ungrilled backlog items; never starts agents without normal approval/admission. The owner walks a round one question at a time; Enter selects a recommendation only at an explicit prompt. No TTY means require supplied payload or return usage; never auto-answer recommendations. Save draft answers server-side; final explicit submit atomically completes the round. Ctrl-C preserves draft and later resumes the next unanswered question. Two channels racing receive 409, reload and show the committed answer; no silent overwrite.

CLI owner authentication (D5): recommend browser/device pairing. CLI requests a short-lived challenge and opens an owner UI approval link; only current authenticated UI owner + CSRF can approve the specific code/client. Exchange a separate random one-time secret for an owner CLI credential (hash stored in owner-only state, fixed 8-hour expiry, explicit revocation, no refresh initially). Polling/challenges are rate-limited and expire in 5 minutes; bind approval to the requested client and code. Worker bearer can create proposals/read permitted data but cannot mint/approve/redeem owner credentials or impersonate the owner. Owner tokens are verified as a distinct principal by require_api, not by adding `ui:` to arbitrary strings. Store client secret in its explicitly chosen private credential store with mode 0600 or Windows owner ACL; never in briefs/shared worker environments. Remote use requires HTTPS through the trusted owner UI path; do not spoof Tailscale headers/Origin or rely on WSL loopback identity. Preserve web CSRF and E9's trust boundary. Owner can revoke a CLI credential in either channel; audit client identity without token contents.

## Grilling and repository framework adapters

A grill is an agent task targeting a versioned artifact/plan/task or selected backlog items. `fields.skill` names exactly one installed `grilling`, `grill-with-docs` or `grill-me` skill; verify it on the selected seat. The brief invokes it with the target, approved read refs, prior decisions and requirement to return numbered questions with recommended answers, rationales and stable IDs. Skill names are references only; no copies of installed skill instructions enter the repository.

Persist grill session ID, target hash, current frontier, round counter, backend session ID (if supported) and handoff artifact. A round publication transaction stores immutable questions and one owner-answer task, requester→answer dependency and notification; key `(grill_id, round_number)` prevents duplicates. Release worker slot while waiting. Answers return as structured JSON plus readable context to the same session only if the backend can verify identity and snapshot compatibility; otherwise fresh run with saved handoff/decisions/type version. No assumption that a CLI process remains alive for hours. New decisions invalidate only affected frontier entries; changed target hash pauses for owner rebase/restart decision. Bound repeated rounds with a configured maximum (default 20); reaching it becomes needs-owner, never false completion.

Finish when the agent reports an empty frontier, all numbered questions have answers, required owner acceptance is recorded, and any glossary/ADR changes have landed. Grill itself stays read-only; propose narrowly scoped docs follow-ups with the exact edits, parent provenance and spec-framework locations. A grill waiting on those docs must not create a reciprocal docs→grill blocking edge. Persist the final frontier/decisions artifact; gate `grill-complete` includes landed follow-ups. Owner may waive with a separate audited waiver action and rationale; no automatic waiver. Attach as a prerequisite/related task to a spec via required action metadata; implementation proposal checks that exact target version's grill result, not an old unrelated completion.

Repository adapter `detect_framework(repo_snapshot) -> {kind,evidence,version}` probes without running commands: OpenSpec if `openspec/specs/` and `openspec/changes/`; Spec Kit if `.specify/` and `specs/` (feature children match `NNN-*` when present); superpowers if `docs/superpowers/specs/` or `docs/superpowers/plans/`; otherwise none. Multiple matches → ambiguous, needs owner override; never pick by incidental directory order. Record detection/ref SHA and effective override per registered repo; `[spec_framework] kind="auto|openspec|spec-kit|superpowers|none"` plus optional artifact root/version in repos config. Drover's own repo is explicitly superpowers by owner direction; do not let an empty directory at this base misclassify it. Reload detection on a new approved repo revision; save chosen adapter binding in the task snapshot.

| Adapter | Artifact and lifecycle contract (proposed integration) |
| --- | --- |
| OpenSpec | Use `openspec/changes/<change-id>/proposal.md`, `design.md`, `tasks.md`, `specs/<capability>/spec.md`; read canonical `openspec/specs/`. Follow repo instructions/schema overrides. After implementation, sync deltas and prepare date-stamped `changes/archive/` move as a separate scoped finalization task. Gate/review and land that commit through queue; verify archived state only after landing, never mutate main from a hook. |
| Spec Kit | Read `.specify/memory/constitution.md` and repo templates; use `specs/NNN-feature/{spec,plan,tasks}.md` (plus framework artifacts such as research.md where required). Honor specify → plan → tasks → implement/check lifecycle; task source is tasks.md. Persist these artifacts; no invented archive step. |
| superpowers | Brainstorm → `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md` → `docs/superpowers/plans/YYYY-MM-DD-<slug>.md` and companion tasks.json. Owner review decisions remain explicit; do not treat generating a plan as implementation approval. This task uses that layout. |
| none | Read repo AGENTS instructions; default spec artifacts to owner-approved `docs/specs/<slug>/` with design.md, plan.md, tasks.json. No framework installation or scaffolding beyond the task's approved files. |

These layout choices follow [OpenSpec's documented overview](https://github.com/Fission-AI/OpenSpec/blob/main/docs/overview.md) (change deltas/archive) and [Spec Kit's workflow](https://github.com/github/spec-kit/blob/main/docs/guides/evolving-specs.md) (spec/plan/tasks/implementation). Repository instructions and installed framework version refine the adapter; if a required command/skill is absent, request owner action rather than install it. Layout adaptation never authorizes write paths outside the approved scope. Spec/research briefs read framework sources; research remains read-only unless explicitly approved to create framework research artifacts; implementation reads approved plan/task source.

Framework task lists import as proposed only. Source identity = repo + framework + feature/change ID + explicit task ID (Spec Kit Tnnn or stable plan item ID); persist source path, content hash and dependencies in import events. For anonymous checkboxes create a durable mapping on first import; moved/reworded ambiguous items become conflicts, never fuzzy silent reassignment. Checkmarks do not prove completion, gates or approval; landed Drover tasks can propose a scoped reconciliation commit to update the source. Honor explicit dependencies, infer sequential phases conservatively, and flag contradictory `[P]`/overlapping scopes for review. Deletion/rewording never cancels owner-edited/live tasks. OpenSpec finalization depends on all change tasks and imports no archived completed items as ready. T-b will consume the same task/source contract.

## Tuning, rollout and verification

Tuner overlays are keyed by `(type_version, role, size)` and new policy_version, with a type's `tunable` allowlist defaulting to routing candidate weights/model preference/effort within approved candidates. It may choose a different default candidate for future runs; changing backend/model outside the ladder requires owner approval as a new type or approved policy. It cannot change fields, access, assignee, lifecycle, required skills, acceptance, gate/review/landing or framework paths. Keep statistics by type and artifact success as well as landed success; no-repo tasks must not poison cost-per-landed metrics (E11). Cold types use configured priors, not unrelated type outcomes. Show effective type+policy diff and allow owner pin/rollback; old runs replay saved execution_json.

Implement in layers: pure contracts/registry + shadow snapshots; transactional persistence/migration; kernel/owner actions; routing/workspace/brief/framework consumers; API/UI/CLI/grill; tuner and compatibility rollout. Do not enable live custom lifecycles until every status query and landing/owner adapter, including HubPort/LandingService (E15), consumes phases and validated actions. The plan/tasks enumerate these boundaries and tests, including clean Windows hub and parser-free WSL client checks.

Risks: migration can lose links/triggers (test v3 and T-7 fixtures with FK checks); state-label queries can skip reservations (audit all callers, restart tests); YAML changes can widen trust (trusted layers and approval diffs); pairing can grant owner rights to a worker (negative auth tests); long-lived grill sessions can duplicate questions or deadlock (idempotency/dependency tests); framework drift can archive prematurely (versioned adapters and explicit finalization commits). Large feature breadth is managed by small independently gated tasks and an opt-in activation flag, not a single dispatcher rewrite.

## Owner decisions

No interactive owner is available for T-9; all recommendations were used to write the plan. They remain decisions for rollout, with no installation or feature activation authorized by this document.

| ID | Question | Recommendation |
| --- | --- | --- |
| D1 | Approve a YAML dependency and its installation footprint? | Approve pinned PyYAML in hub only; keep normalized workers/CLI stdlib-only; reject handwritten subset parser. Dependency task stays held until approved. |
| D2 | Can repo content define privileged types/hooks? | Only owner-configured repo layers outside worker checkouts; declarative actions only, core invariants immutable. |
| D3 | Research/investigation defaults: docs commits or read-only results? | Read-only hub artifacts; use spec or an approved write override for committed research. Preserve T-7 explicit write values. |
| D4 | How freely can types replace lifecycles? | Permit custom state names/graphs with fixed kernel phases, actor checks and mandatory delivery evidence. |
| D5 | How does CLI gain owner authority? | Explicit owner UI device pairing with expiring/revocable separate credentials; no reuse of worker bearer or fabricated UI identity. |
| D6 | Reuse questions storage or promote owner work? | Promote to owner tasks; retain questions as a synchronized compatibility facade with one mapping and one action service. |
| D7 | How are grill edits and completion controlled? | Read-only rounds plus docs follow-ups landing glossary/ADR commits; empty frontier and owner acceptance required; max 20 rounds before escalation. |
| D8 | What happens when frameworks conflict or require tooling? | Require repo override on ambiguity, honor local version/instructions, never install; archive only through reviewed finalization commits. |
| D9 | May tuner rewrite type defaults? | Versioned routing-only overlays within an owner-approved candidate allowlist; task type versions immutable; expose diff/pin/rollback. |
| D10 | Can low-risk docs/mechanical work skip checks? | Mechanical always full gate; docs scoped with review by default; permit owner-approved docs-only no-code exemption, never generic code bypass. |
| D11 | Should existing tasks/runs adopt new definitions automatically? | No; pin at creation, explicit pending-task upgrades and legacy draining before activation. |
