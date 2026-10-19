# CI-parity local gates, triage, review and landing

Date: 2026-10-19. Task: T-ce. Status: proposed implementation contract; owner decisions G1–G4 are approved. This document authorizes no live configuration changes, installs, services, publication or merges.

## Intent and decisions

Drover should spend compute proving a candidate and spend model tokens where judgement helps. For solo repos, derive build and test gates from GitHub workflows and execute them locally, without GitHub or an agent executing the gate. The Windows host and the registered WSL Ubuntu distro are the CI environments. Docker images and a general GitHub Actions emulator are future work.

| Decision | Required behavior |
| --- | --- |
| G1: local CI parity | Derive the same required build/test jobs, matrix variants and prerequisites from workflows. Map Windows jobs to Windows and Ubuntu jobs to WSL. Report every unsupported obligation and environment difference; never call a partial check full parity. |
| G2: mechanical green landing | Docs-only and mechanical tasks skip agent review. Reuse valid review evidence for land-candidates. New agent-written code receives one Sol review before landing; HOLD fixes receive focused re-review, without another review merely for entering the landing queue. |
| G3: cheap failure triage | Luna or Haiku sees a bounded failing excerpt first. It proposes flaky, environment/sandbox, already failing on main, or change-caused routing. Deterministic validation controls the action. Only change-caused failures go back to the worker, with at most ten summary lines. |
| G4: one logged chain | Gate attempts, triage, retries, flakes, review/fix cycles and merge decisions use the E-v self-improving harness events and queries (T-bp/T-bq), with cause links, pinned policy and measured costs. |

Observed motivations, supplied by the owner: gate 226's flaky Windows runner test needed manual triage; T-1h twice produced no output for 600 seconds on Windows; motif T-6m's gate 264 failed on changed ADR 0043 but returned only “blocked”; T-c needed four review cycles with one new finding each; already-reviewed land-candidates were reviewed again. These are acceptance scenarios, not retrospective machine-verified classifications.

### Grounding and integration boundaries

The inspected worktree starts at `4157093`; the supplied read-only main snapshot has no E-v, T-bi or T-cd design artifacts. Their exact event identifiers and shared interfaces must be reconciled when integrating, rather than invented as already-landed facilities. Section 5 defines the semantic events, dimensions and queries this feature requires; E-v remains the authority for the common envelope, taxonomy, cost accounting and query registry. T-bi supplies the shared stuck/failure triage rung; T-cd supplies Windows Execution Containers. These are dependencies, not facilities this document claims exist today.

Current repo evidence:

- `drover/repos.py` reads per-repo registrations, validates `[run].platforms`, and resolves shared/platform-specific `full` and `scoped` gate argv. `unknown` prevents inventing a command.
- `drover/gate.py` and `drover/gatejob.py` bind receipts to exact SHAs and clean, unchanged trees, parse failures and support existing flaky-test retries. The latter's command loop alone does not establish workflow parity or a nonempty gate.
- `drover/dispatcher.py` sequences Windows then WSL, records gate/review runs, asks the currently shadow `gate_failure_class` question, and starts reviews after gates. `decide-criteria.json` currently combines environment and flakiness under `env_flaky`.
- `drover/landing.py` requires reviewed candidates, replays them on a fresh base, gates the batch tip and lands through the queue. `drover/contract.py` defines existing gate/outcome contracts; `task_events`, `decisions` and `run_stats` already hold history and usage.
- This checkout has no `.github/workflows` directory. The supplied snapshot contains only fictional repo registrations, not the live motif, drover or PanGloss registrations. Migration must inventory those actual inputs; this spec does not assert their contents.

Only the Windows hub owns the ledger. WSL execution reports through existing hub boundaries. No second ledger, telemetry store, policy engine or agent scheduler is introduced.

## 1. Deriving a gate from workflows

### Inputs and required jobs

At the exact candidate SHA, parse `.github/workflows/*.yml` and `*.yaml` as data. Use a bounded YAML parser with safe loading, duplicate-key rejection and no custom object constructors. YAML parsing is an implementation dependency decision; do not add a package implicitly. Preserve source file, job/step ID and line references in diagnostics.

Build a normalized job graph for the repo's declared validation event context, normally a PR against its configured main branch, plus any validation jobs required only on push to main. Job names are hints, not an exclusion mechanism. Include build, compile, test, lint and repo validation jobs and their transitive `needs` dependencies, including reusable-workflow prerequisites. Publishing, release and deployment jobs are listed as excluded with a reason. Ambiguous jobs remain unresolved until a repo override classifies them. A job cannot disappear merely because its name lacks “test”.

Expand finite matrices including `include` and `exclude`, `runs-on` expressions and configuration/runtime variants. Preserve dependency ordering, workflow/job defaults, environment, shell, working directory and timeout semantics. Bound expansion to 64 variants by default; overflow is unsupported pending an explicit limit/configuration change. Duplicate test commands on Windows and Ubuntu remain separate obligations. Fail-fast may stop scheduling after red, but unexecuted variants remain incomplete.

Evaluate only a documented expression subset: literal booleans/comparisons, finite matrix values, registered event/ref/path facts, and explicit success/failure/always conditions. Step outputs, dynamic matrices, arbitrary functions and unresolved contexts are unsupported unless an explicit adapter handles them. A condition proved false is recorded with its inputs and reason; unknown is never treated as false. Event/path exclusions must be reported. A docs-only diff may skip jobs only when workflow semantics prove that skip; an empty selection is an explicit `not_applicable` result with evidence, never a passing zero-command gate. A solo repo with no applicable validation needs an explicit owner-approved no-check policy before mechanical landing.

### Placement and translation

| Workflow construct | Local treatment |
| --- | --- |
| `runs-on: windows-*` | Registered Windows placement and its capability inventory. |
| `runs-on: ubuntu-*` | Registered WSL Ubuntu distro and its capability inventory. |
| `macos-*`, unregistered self-hosted labels, `container:` | Unsupported placement; no substitution with Windows/WSL. |
| `run:` | Preserve script content and the workflow-selected shell, render only supported expressions, and hash both source and rendered script; invoke an approved shell by argv in an isolated checkout. No task-text interpolation. |
| Checkout action | Bounded adapter checks out the exact SHA. Nondefault depth, submodules, LFS and secondary repos require implemented equivalents or are unsupported. Never edit the owner's checkout. |
| Runtime/toolchain setup action | Adapter verifies the requested installed version and options, or reports missing capability. It does not silently use PATH's version or install anything. |
| Cache action | Optional acceleration only when its absence cannot alter correctness. Record hit/miss and keys; cold execution must still work. |
| Artifact upload/download | Upload may become a local hashed artifact receipt if only retention is needed. Cross-job downloads require a real local producer/consumer adapter; never skip a needed artifact. |
| Other marketplace actions, local/composite actions, reusable workflows | Unsupported by default. Explicit version-bound adapters may expand inspected behavior into local commands; recurse into local files only with bounded cycle/depth checks. Remote action code is never fetched and executed implicitly. |
| `services`, secrets, authenticated external calls | Unsupported by default; report names/requirements without values. An owner-configured service/credential adapter is needed, with explicit authority and availability. No service startup, secret copying, or install as a side effect of derivation. |

Honor GitHub's default shell behavior through explicit local shell adapters; merely running every command in the hub's shell is not parity. Each step gets its own process. Implement supported environment/output-file propagation and job outputs explicitly; otherwise flag them unsupported. Quote expression values as data according to the selected shell or reject the expression; do not concatenate untrusted refs, event text or paths into shell code.

This is a constrained workflow compiler. It emits a plan and diagnostics without an LLM. Unknown syntax fails closed. The plan is immutable and content-addressed, with candidate/base SHAs, workflow file digests, compiler/adapter versions, selected/excluded jobs and conditions, matrix values, placements, script/argv hashes, environment requirements, prerequisites, timeouts and artifact edges.

### Parity report and overrides

Report two coverage fractions and an environment assessment:

- Job coverage = fully supported required job variants / all required job variants.
- Obligation coverage = implemented required obligations / all required obligations. Obligations include placement, toolchain/setup, every behavior-bearing step, condition, service/secret and dependency/artifact edge. Count a blocked or unresolved obligation in the denominator. A mapped-but-unavailable capability is not implemented for this run.
- `parity_score = floor(100 * min(job_coverage, obligation_coverage))`. Zero required variants yields `not_applicable`, not 100. An unexpandable matrix/graph yields `unknown`, with unresolved counts, not a guessed percentage.
- Record requested OS labels and runtime versions beside actual Windows build, WSL distro release and toolchain versions. Host/distro placement implements G1; it does not assert byte-for-byte identity with a GitHub hosted image. Unmet pinned OS/runtime requirements block parity. Owner-accepted label differences are visible deviations, never proof of exact image identity.

A score of 100 means all required logical obligations have local equivalents and required capabilities; an unsupported required step prevents authoritative green. Explicit waivers do not inflate coverage. A solo repo needing a waiver remains a partial pre-check until the missing adapter is supplied or the owner approves a different, visibly non-parity gate policy. That exception is versioned and logged; it cannot be inferred from a successful legacy command.

Keep overrides in the hub-owned `~/.drover/repos/<repo>.toml`, resolved from the hub state directory, with platform-native paths only at execution. Extend the existing gate schema rather than replacing its command tables. The proposed configuration surface is:

```toml
[gate.workflow]
mode = "shadow" # shadow, authoritative, precheck; legacy command mode remains readable
include = [".github/workflows/*.yml", ".github/workflows/*.yaml"]
event = "pull_request"
required_jobs = ["ci.yml::build", "ci.yml::test"] # additive; dependencies are included

[gate.workflow.overrides."ci.yml::test"]
reason = "Use the already installed toolchain; same version and options"
source_sha256 = "<digest of the overridden job definition>"
adapter = "installed-toolchain-v1"
```

This example defines future fields, not a registration to copy into today's parser. Overrides can add required jobs, classify exclusions, select registered placements, bind adapters and declare local equivalents. Any command replacement must include its reason, exact source digest, owner/policy provenance and validation evidence. Additional local checks are labeled local-only. Repo-local workflow changes cannot self-approve an override or exclude checks by editing hub policy. Secrets are referenced by capability name, never stored in this TOML or a plan. Existing `[gate.commands]` can supply transitional equivalents but must be matched to source obligations before being called parity.

Derive and compare on every workflow addition, edit, deletion or rename, and on adapter, override or capability changes. Compare both main and candidate plans to detect deleted or weakened checks. Changes to workflow-discovered local action files, reusable workflows or gate scripts also invalidate the fingerprint. Emit a drift report with added/removed variants, altered commands/toolchains/placements and stale overrides. A changed source digest invalidates the corresponding override. Rederive against the landing tip; policy drift, unapproved weakening or unsupported new steps holds authoritative landing. A clean candidate cannot reuse a receipt generated under a different plan/policy/environment fingerprint.

## 2. Executing on Windows and WSL

The hub schedules plain local gate runs (`backend=local`, no model, no agent seat). Admission controls build capacity and disk/memory trackers; deriving multiple jobs never authorizes extra parallel builds. Initially execute topologically and sequentially, Windows then WSL where dependencies permit. Missing WSL or a required Windows capability holds the affected gate with a precise systemic reason. Worker placement cannot downgrade the derived gate placements or bypass `[run].platforms`; configuration mismatches require correction.

Prepare a clean, task-owned checkout for each job that needs isolated workspace state. Jobs share only explicitly declared artifacts. Do not depend on another job's ignored build outputs or warm workspace. Keep generated scripts, receipts and logs outside tracked source. Gates verify exact HEAD and a clean tree before and after execution; tracked generation requires a designated verification step and must leave the tree clean. An empty command list, missing receipt, timeout, lost process, dirty tree or unexecuted required variant is never green.

### Restore and sandbox boundary

Split preparation from build/test without dropping restore obligations:

1. Pin candidate, plan, lockfiles, toolchains and placement. Check available disk before heavy work and acquire the existing gate lease.
2. Pre-restore/download approved dependencies outside any sandbox, under a bounded preparation command and scoped credential/network authority. Record commands, exit status, dependency/lock hashes, cache manifest and logs. Do not run arbitrary candidate-controlled build hooks with hub credentials: restore adapters must suppress hooks/evaluation or perform acquisition in an unprivileged preparation context without owner credentials. A restore that cannot be separated safely is unsupported until an adapter exists.
3. Use per-platform caches keyed by toolchain and dependency inputs. Recreate required local assets in the isolated job workspace; Windows/WSL paths and native outputs are not interchangeable. A changed lockfile or SDK invalidates preparation. Missing packages are preparation failures, not worker blame.
4. Execute build/test using restored inputs with the ecosystem's no-restore/offline equivalent where supported. Record that the workflow's restore obligation was fulfilled in preparation. Network-dependent tests require an explicit capability; sandbox denial is not silently solved by disabling isolation.

Windows initially uses the existing plain local runner with an explicit `isolation=host` receipt. Once T-cd is integrated, gate execution runs inside its Execution Container with the prepared source/dependencies and declared capabilities; record the container/profile identity. Preparation remains outside that boundary. Before/after T-cd evidence has different environment fingerprints and is not interchangeable. A repo requiring container isolation waits if T-cd is unavailable, rather than silently falling back. WSL jobs execute in the registered Ubuntu distro through the existing WSL runner boundary. Docker is deferred.

Stream stdout/stderr to durable per-step logs with job/step boundaries, timestamps and exit status, while emitting a runner heartbeat at least every 30 seconds independently of command output. Preserve separate launch, preparation, command-output and heartbeat times. Honor workflow timeouts, capped by registered repo limits; absent a workflow limit, retain the repo's gate timeout. Retain its idle-output limit too, but treat output silence as a triage signal distinct from a dead runner. The T-bi rung receives a 600-second no-output signal by default; this does not extend a shorter existing timeout or kill an active command merely for silence. Rung polling cannot extend the absolute deadline. Timeout cleanup addresses only recorded gate PIDs and their owned process tree/container; never another repo's process.

Each job receipt includes run/attempt IDs, exact SHA/base, plan/policy/workflow/override/environment fingerprints, placement, preparation receipt, executed step statuses, artifact/log digests, timing, tree checks and any retry link. Aggregate green requires every required variant to pass or have an evidenced workflow-semantic skip, plus the applicable parity policy. Diagnostic filtered tests and baseline probes are separate receipts; neither can substitute for the required gate. Previously passed jobs may be reused only at the same SHA and fingerprints with intact required artifacts; a retry executes the entire failed job and its invalidated dependencies, not only a failing test class.

## 3. Review eligibility and mechanical landing

Determine eligibility from task kind **and the actual diff**, provenance and pinned repo policy. A worker's “mechanical” label is not authority. Record the result as review-required, review-exempt or review-reused; do not fabricate a Sol run or `LAND@sha` for an exemption.

| Candidate | Low risk | High risk / safety boundary |
| --- | --- | --- |
| Docs-only | Skip agent review after confirming only allowed documentation paths/content changed; full applicable gate still required. | Same exemption for genuine documentation. Existing explicit owner holds and approval requirements remain. Executable snippets consumed by tooling, workflow/config changes and generated runtime inputs do not qualify just because they live under `docs/`. |
| Mechanical | Skip agent review for a declared, deterministic transform with mechanically verified before/after invariants and scope. | Skip only when the same invariants actually prove the transform and required owner authorization is already recorded. Semantic/ambiguous transforms are code changes and need Sol. |
| Already-reviewed land-candidate | Reuse valid review coverage; no new review because the task is now landing. | Reuse only if the review met the applicable risk policy and no new risk/owner hold invalidates it. |
| New agent-written code, including fixes | One Sol review of the complete candidate diff after green. | Sol remains required, plus any existing owner/high-risk checks. Do not substitute Luna/Haiku triage for code review. |
| Unknown kind/provenance or invalid evidence | Require Sol or obtain missing evidence. | Hold when required authorization/evidence is missing. |

Docs-only eligibility uses a repo-owned allowlist plus content/consumer checks, excludes symlink/mode changes and mixed code changes, and never means “skip CI.” A mechanical transform supplies a registered verifier; a model's assertion of equivalence is insufficient. Unsafe classification defaults to review-required. Risk changes cannot be made solely to obtain an exemption. No exemption bypasses scope, task acceptance, dependency ordering, explicit owner holds, destructive-operation authorization, required platform gates or the team-repo policy below.

Review receipts pin task revision, full reviewed SHA, base, canonical changed-file fingerprints, reviewer/model, policy/risk, findings and verdict. On ordinary exact-SHA reuse, validate these against the current candidate and outstanding holds. For a conflict-free landing replay onto a newer base, reuse coverage only with mechanical proof that every changed path's before/after blob IDs and modes are identical to the reviewed change, with no added/removed paths or edited conflict resolutions. Record the original review and replay proof; never relabel the old review as having read the new SHA. Changes elsewhere on main still require a full gate on the new landing tip. Changed preimages, conflict resolution, changed requirements/risk, code edits or unverifiable review provenance require a fresh Sol review of the affected candidate. A historical review by a different reviewer needs an explicit policy compatibility rule; “reviewed before” alone is insufficient.

The first Sol review should report all material findings it can establish in one pass, with stable finding IDs. HOLD → worker fix → green gate → focused Sol re-review links those IDs and checks the fix plus new regressions introduced by it. Re-review may report newly established material issues, but records why they were not found earlier. Persist every round, including incomplete review attempts. After two HOLD/fix rounds, route to the shared stuck triage for a summary and escalation decision instead of an unbounded automatic loop. It cannot turn HOLD into LAND. An accepted LAND is reused through queue entry; there is no extra review of every land-candidate.

For solo repos, the landing queue mechanically lands an eligible green candidate (or compatible batch) by fast-forward only. Under its lock, validate scope, holds, required review/exemption, exact tested tip, clean tree and fresh plan/policy/environment evidence, then compare main to the expected base immediately before updating. If main moves, rebuild/re-gate the tip; reuse review only by the proof above. Batch red follows the same triage before attributing blame or dropping a task; uncertain attribution uses existing batch diagnosis/bisection, not an arbitrary worker retry. Never reuse a task receipt as a batch-tip receipt. Record the merge decision, attempted effect and verified resulting SHA, including a crash/reconciliation outcome. Push/publish permission is unchanged; green does not authorize those effects. Workers still never merge themselves.

## 4. Shared cheap triage rung

Gate red and stuck/no-output signals enter the same T-bi bounded triage service with different `trigger` values. The service is a short classification call to an admitted Luna or Haiku seat, not a repository-editing worker and not part of gate execution. It cannot run commands, browse the full log, mutate files, grant permissions or merge. The hub mechanically extracts and redacts only failing sections first; success output, complete transcripts and full repo diffs are excluded.

Record raw gate failure immediately, but defer worker retry, attempt penalties and attribution-based quality metrics until triage validates the cause. The current `_failed` path must not run first and charge the worker before this rung. Preserve raw gate reliability statistics independently of worker blame.

Input includes task/run/step IDs, trigger, exit/timeout/heartbeat facts, failing test/error/file:line records, changed-path membership, failure signature, rerun count, placement/environment and any already-known baseline evidence. Bound initial input, including instructions/schema, to 2,048 tokens and output to 512 tokens, with a 60-second call deadline. At most one supplementary failing excerpt is allowed (1,024 input, 256 output); the total episode ceiling is 3,840 tokens and 120 seconds of model-call time. T-bi and gate triage share that ceiling for the same failure episode. A retry of an ambiguous model request counts against it; no hidden chain of reasoning calls. Persist configured caps, measured tokens, model/seat and timeout. No eligible cheap seat, malformed output, low confidence or exhausted budget yields `unknown` and a bounded escalation record, not a stronger full-log review by default.

The semantic output schema is versioned; E-v supplies its envelope and T-bi owns the shared transport. Every key below is required, nullable fields remain explicit, and unknown keys/oversize values are rejected. This example describes a hypothetical ADR validator failure:

```json
{
  "schema_version": 2,
  "trigger": "gate_failure",
  "gate_run_id": 9001,
  "failure_id": "step-3:adr-0043",
  "classification": "change_caused",
  "confidence": 0.93,
  "failure_kind": "test_logic",
  "test": "ADR validation",
  "error": "Changed ADR violates the repository validator",
  "file": "docs/adr/0043.md",
  "line": null,
  "evidence_refs": ["gate:9001/step:3/log:lines:120-134"],
  "baseline_receipt_id": null,
  "systemic_task_id": null,
  "suggested_action": "worker_fix",
  "summary_lines": ["ADR validation: changed ADR violates repository validator; docs/adr/0043.md (line unavailable)."]
}
```

Enums: `trigger` = `gate_failure|gate_stuck`; `classification` = `flaky|environment_sandbox|baseline|change_caused|unknown`; `suggested_action` = `rerun_once|link_systemic_fix|check_baseline|worker_fix|escalate`. `failure_kind` retains the legacy detail enum `syntax|type|test_logic|build_system|env_flaky|scope_violation`, or null; it does not control attribution. Confidence is in [0,1], summary has at most ten lines and 1,500 characters total, error at most 300 characters, and evidence references must belong to this episode. File paths are repo-relative or null, line is a positive integer or null; never invent a location. Store separate results for independent failures under one gate episode. All must be resolved before green; flaky routing for one cannot hide another deterministic failure.

Promote `gate_failure_class` from shadow to live using a new criteria version for the routing enum above, retaining detail classes separately. Old answers remain under their original criteria version; `env_flaky` history cannot be retroactively split without evidence. A live verdict requires confidence ≥0.8 and a valid evidence-backed action. Otherwise classify unknown and escalate with the bounded excerpt. Keep the existing decision ID, rule answer and eventual outcome links. The model proposes; hub rules validate baseline and rerun eligibility, deduplicate work and execute the action.

| Verdict | Deterministic action and task treatment |
| --- | --- |
| Flaky | Rerun once, mechanically, at the identical SHA, plan and environment. One automatic flaky rerun per gate episode, shared with legacy retry facilities; disable nested per-test retries. Rerun the failed job and invalidated dependencies. A pass records an observed flake and `passed_with_flakes`; only then can the aggregate gate become green. Another failure exhausts the allowance and needs diagnosis, not another automatic retry. |
| Environment or sandbox | Attach to an existing systemic fix or file one, deduplicated by repo/capability/failure signature. Record disk, missing toolchain, restore, container denial or runner-health evidence. Preserve the candidate; do not debit worker attempts or reduce worker quality scores. Wait for the prerequisite and then run a fresh gate; never escape the sandbox automatically. |
| Already failing on main | Require a comparable existing baseline receipt or run one bounded diagnostic on the exact recorded main SHA in an isolated checkout, with the same failing check, inputs and environment. Matching failure signature confirms baseline; a green/different/incomparable result does not. File/link a separate fix; leave this worker's code, attempts and blame alone. Preserve candidate evidence while waiting. Baseline failure is not permission to merge red. |
| Caused by this change | Return at most ten lines and 1,500 characters total, including test/check, error, file:line (or explicitly unavailable), evidence links and failure ID. Resume the responsible worker through the existing retry policy. Do not send the full log or only “blocked.” Changed-file membership is evidence, not proof of causation by itself. |
| Unknown / conflicting evidence | Keep the task pending diagnosis with a specific reason and excerpt reference. Escalate through T-bi's bounded escalation path; missing output alone is neither flaky nor a code defect. No blind worker rerun. |

Budget one baseline probe per failure signature, main SHA, plan and environment; reuse comparable results and schedule through normal gate admission. A probe has the same command/time/resource bounds as its gate job and is labeled diagnostic, never candidate success. A suggested baseline verdict without proof first executes `check_baseline`; its result appends confirmed/rejected attribution. Known baseline code may fail under a different candidate workflow: record incomparable evidence and escalate instead of blaming or clearing the task.

T-bi handles a live-but-silent process using heartbeat/PID facts and the remaining absolute timeout. It may advise waiting within that deadline; only the deterministic runner cancels an owned process at timeout or an authorized stop. A runner still active after a launch timeout is reconciled by its run ID before any new launch. Repeated T-1h-style hangs attach to one systemic issue with both attempts and timings. UI state must distinguish waiting on systemic repair, waiting on baseline repair, worker fix, and triage unknown; no generic blocked label that loses the reason.

Decision correctness uses later evidence, not “next run passed” alone: flaky requires a same-input red→green pair; baseline requires the comparable baseline signature; environment requires repair/recheck evidence; change-caused requires linked fix/recheck evidence. A next run with different code or environment cannot by itself validate the original attribution. Append corrections and keep unconfirmed outcomes unknown.

## 5. E-v events, cause links and named queries

Use the common E-v taxonomy and append-only event writer/query registry from T-bp/T-bq. The operation labels below are semantic requirements to bind to that registry, not a second independent taxonomy or an assertion of its unavailable literal enum names. Integration must register a single mapping and schema version; producers, query fixtures and compatibility tests use that same mapping. Do not ship a parallel `ci_events` table or a second usage total. Preserve existing `gate`, `gate-started`, `gate-done`, `review-done`, decision and landing consumers through an adapter/projection; do not double-count compatibility events.

Required common dimensions: event ID/version/time, actor and source, epic/task/repo, task revision, worker/gate/review/triage run IDs as applicable, policy and criteria versions, SHA/base and plan/environment fingerprints, correlation/root episode ID, immediate `cause_event_id` plus additional causal references, decision/action/receipt IDs, outcome/reason, artifact hash/location and measured cost references. Use E-v's canonical field names/types when binding these dimensions. Null means unknown; never create a worker/session to stand in for a local gate. A systemic task creation links both the failure episode and systemic task; a batch links all member tasks and its own tested tip.

| E-v semantic category | Operations required from this feature | Required causal evidence |
| --- | --- | --- |
| Observation / execution | Plan derived/drift found; preparation started/finished; gate/job/step started/finished, skipped, refused, timed out or lost | Source digests, capabilities, placement, actual commands, logs, duration, exit/status and exact receipt. Every attempt, including zero-output failure, has terminal or explicit unresolved evidence. |
| Decision | Triage requested/verdict/validated or rejected/corrected; review eligibility; review verdict; merge allow/hold | Evidence slice references, model versus rule answer, confidence/criteria, caps/cost; review proof or exemption; required-receipt set and hold reasons. |
| Action | Rerun requested/started; systemic or baseline fix linked/filed; worker fix requested/completed; review/re-review started; merge attempted | Triggering decision, stable action/idempotency key, predecessor attempt, task/finding IDs and expected SHA/base. |
| Outcome / feedback | Rerun completed, flake observed/confirmed, attribution confirmed/rejected, review finding resolved/new, merge succeeded/refused/reconciled | Parent action and original red run; same-input proof or change details; resulting SHA and verification. Local gates carry zero model tokens, not unknown model usage. |

Example chain: gate red → triage verdict → validated rerun decision → rerun action → gate green → observed flake → review-exempt/reused decision → merge allow → merge attempt → verified merge. A HOLD chain is review verdict/finding → worker fix → new SHA/gate → re-review → finding resolved/verdict. Link every round to the initial review episode and preceding finding/fix, preserving T-c's four-round shape for analysis. Review reuse and skipping are explicit decisions, not extra review cycles.

Persist launch/action intent before effects, results afterward, and append the state transition with its event in the hub transaction where possible. A failed log/event write cannot yield an unlogged green or unlogged merge permission. External process/merge effects cannot share a SQLite transaction: reconcile after restart by stable action/run ID, runner status and expected/resulting refs. Ambiguous intent is a reconciliation hold, not a blind rerun or second merge. Dedupe observations by source/run/step/attempt/phase key while preserving genuinely distinct attempts. Event replay rebuilds projections without scheduling effects.

Keep bounded, redacted failure slices and their hashes with permanent event evidence; full logs/artifacts follow E-v's retention policy. If a log expires, retain its hash, slice and explicit expiry marker, never a dangling claim that it was inspected. Secrets never appear in event payloads. Corrected verdicts supersede projections but do not edit history. Model usage is recorded once per actual call/run via the canonical usage source, including cache fields according to backend semantics; no estimated zero for missing measurements. Preparation and local retries have zero tokens and measured wall time.

Register these named queries in E-v's query catalog, with `since`, `until`, optional `repo`, `task` and `policy_version` filters and evidence IDs in result rows:

| Query name | Definition and output |
| --- | --- |
| `flaky_tests_by_frequency` | Group confirmed same-input red→green observations by repo, placement, job/matrix and stable test ID. Return flake count, observed test execution count, frequency when that denominator is known, first/last time and sample red/retry receipts. Job-only failures use a job signature and are labeled separately; missing test counts yield null frequency. Count episodes once, not retry starts or duplicate compatibility events. |
| `review_cycles_per_task` | Per task, return review attempts, completed verdict rounds, HOLD count, completed HOLD→fix→re-review cycles, new/resolved findings per round and review tokens. Exclude skipped/reused eligibility decisions from review rounds, and report incomplete cycles separately. T-c's supplied four completed review rounds should appear as four rounds, not eight reviews because of queue entry. |
| `gate_fail_classes_by_repo` | Count original red gate episodes by latest validated classification and policy/criteria version, with raw-verdict counts and unknown/unconfirmed counts separate. Return placement, signatures and linked systemic/baseline tasks. Do not count the same failed retry as a new original episode; offer attempt counts separately. Historical `env_flaky` remains a legacy bucket unless later evidence supports a correction. |
| `tokens_spent_on_reruns` | Follow rerun/rework cause links and sum unique canonical usage IDs for triage, repeated worker work and re-review; separate each category by repo/task/model/reason. Report local gate retry tokens as zero, local wall time separately, and unknown usage counts. Count each usage record once even when it links to several events; exclude unrelated initial work and initial reviews. |

Queries must support both “as observed then” and “latest corrected attribution” without rewriting policy history. Their fixtures cover zero tokens, unknown costs, multiple failures, retries, correction, duplicate delivery and cross-task/systemic links. This is the G4 optimization record; a future tuning pass may propose changes but cannot silently change active retry caps, reviewer policy or approvals.

## 6. Team-repo exception

FieldWorks, liblcm and machine use the registered team/PR delivery policy. GitHub CI on the PR is the real check; derived local gates are pre-checks and clearly labeled `authority=precheck`. Local green cannot complete these tasks, stand in for required PR checks or cause a local/main merge. The compiler may report unsupported local jobs while the PR runs them; that does not waive any GitHub requirement.

Use the existing [team-repo design](2026-10-17-team-repos-prs-design.md): pin remote evidence to host/repo/PR, current head/base and applicable rules/check requirements, obey human review/merge policy, and confirm the actual GitHub merge SHA. Missing, stale, pending or inaccessible required checks remain unknown; do not infer success from a local pass or an empty check list. Rerunning GitHub jobs is an external effect requiring the registered permission path; G3's mechanical local rerun rule does not grant it. Record observed remote failures/reruns with GitHub IDs and observation times, without claiming a complete remote audit trail. Triage must obey the same excerpt/secret/cost limits.

Register the exception explicitly per repo, including liblcm; names are not automatic permissions or a reason to convert existing tasks silently. If PR delivery is not implemented/enabled, hold rollout for these repos rather than falling back to solo landing. G2's review exemptions affect Drover's internal review only; GitHub protections and eligible human reviews remain mandatory. Push, PR creation and publication retain their existing approval rules.

## 7. Migration: motif, drover and PanGloss

Migrate one solo repo at a time, retaining the old config and receipts for rollback. Do not edit live registrations as part of this documentation task. Proposed fields above need parser support before activation.

1. Inventory each repo's actual workflows, required checks, registered platforms, shared/platform `full` and `scoped` commands, scope maps, restore behavior, timeouts, retries and review policy. Save the input digests and compare legacy coverage to the derived graph. No workflows means `parity=unknown/no_source`, not a synthetic 100; keep a visibly manual legacy gate until workflows are supplied or the owner approves a non-parity policy.
2. Add derivation/reporting in shadow mode. The current authoritative gate continues deciding landing. Compare commands/variants and runtime capability inventories; fix unsupported equivalents without launching parallel heavy builds. Shadow comparisons use admitted sequential runs and cannot initiate merges or double retries.
3. Enable E-v recording and the shared triage service behind independent flags. Verify record completeness and action deduplication before making `gate_failure_class` live. Drain active failure episodes or explicitly pin them to the old criteria; no mixed retry counters.
4. Validate each required placement using the real host/distro, cold preparation and restore caches, plus an injected ordinary failure, timeout, missing capability and drift. Record the exact plan/config/environment and passing gate receipts. Enable authoritative workflow gates only with complete parity or a separately approved, visibly non-parity policy. Preserve legacy config fields for older pinned tasks, but do not silently fall back after an authoritative gate fails.
5. Enable G2 eligibility/review reuse and queue landing after its evidence checks pass. Migrate idle tasks with explicit policy/version events; active runs retain their original policy until a controlled handoff. Changed/replayed candidates always gate the final tip.

| Repo | Specific migration work and rollout evidence |
| --- | --- |
| motif | Inventory the real workflow graph and legacy build/test/ADR checks; preserve ADR validation as a required obligation where configured. Replay the shape of T-6m/gate 264 with an invalid changed ADR and verify that triage returns the validator, message and actual file:line (or explicitly unavailable) within ten lines. Do not assume all motif checks are .NET or that docs can skip its validator. |
| drover | The documented current full suite is `python3 -m unittest discover -s tests -t .`; platform-specific installed Python argv may differ. The supplied tree has no workflows, so workflow parity cannot yet be asserted. Inventory the live registration and add/locate canonical workflow definitions in a separate task before authoritative parity rollout. Preserve Windows runner tests and WSL coverage where required; reproduce gate-226-style flakiness and T-1h-style silent hangs with fixtures and real-placement validation. Verify mechanical/docs exemption, one Sol review for new code and reuse of an accepted land-candidate review. |
| PanGloss | Inventory actual workflows/toolchains and current per-platform full/scoped commands; no commands or supported placements are inferred from its name. Resolve every missing adapter/capability, compare the full variant set and cold restore, then pilot on its registered placements. Scoped legacy checks stay pre-checks unless a workflow-equivalent selection proves their coverage. |

Rollback disables the new routing/authoritative mode for new episodes through a versioned policy change; it does not delete events, mark a red result green, reset rerun allowances or reuse receipts under a changed plan. In-flight effects reconcile first. A missing E-v/T-bi/T-cd integration is reported as a prerequisite, not papered over with a second implementation.

## Verification and small implementation handoff

Each row below is independently landable, with new behavior disabled until its prerequisites and rollout checks exist. IDs are local handoff labels, not allocated ledger task IDs. No product implementation or task creation is included in T-ce.

| ID | Small deliverable | Dependencies and acceptance evidence |
| --- | --- | --- |
| I1 | Shared-contract reconciliation: bind this spec's event/query operations and triage fields to E-v (T-bp/T-bq) and T-bi; document T-cd runner/preparation interface. | Read their approved artifacts first; land contract/fixture mappings without activating routing. Exact common taxonomy/schema validation must pass; no duplicate event/cost store. |
| I2 | Safe workflow discovery/parser and immutable normalized plan. | I1 for event output; fixtures for build/test discovery, dependencies, finite matrix expansion, YAML booleans/duplicates, bounded recursion/expansion and unresolved conditions. Derivation only, no execution. Resolve parser dependency policy explicitly. |
| I3 | Repo override schema, parity report and drift detection. | I2; legacy registrations still parse. Fixtures prove unknown is not 100, stale overrides/deleted checks hold, and workflow/local-action/script changes invalidate fingerprints. |
| I4 | Shell, checkout and installed-toolchain adapters. | I2/I3; golden plans preserve shell/env/cwd/runtime variants. Unsupported marketplace actions/services/secrets are reported; no implicit install/fetch/service startup. |
| I5 | Host/WSL job executor and aggregate receipts. | I4; same-SHA clean-tree validation, nonempty gate, topological jobs/artifacts, admission, timeouts, heartbeat and restart adoption verified with fake processes. Obtain admitted, agent-free Windows/WSL gate receipts before activation. |
| I6 | Pre-restore and capability/cache receipts. | I5; one ecosystem adapter per follow-up task if needed. Cold-cache, dependency change, credential isolation and no-restore build evidence; unsupported restore hooks fail closed. |
| I7 | Windows Execution Container gate adapter. | I5/I6 and T-cd; preparation stays outside, build/test inside, isolation/profile fingerprint recorded. Missing required container never falls back silently. |
| I8 | Bounded shared triage call and live decision projection, initially actionless. | I1/I5; Luna/Haiku budgets/schema/confidence enforcement, redaction and cause/usage links. Malformed/timeout outputs stay unknown; legacy criteria remain readable. |
| I9 | Mechanical flaky rerun with one shared allowance. | I8; red→green fixture confirms a flake, red→red stops, multiple failures remain accounted for, restart cannot launch a second retry, filtered-test success cannot clear the gate. |
| I10 | Systemic/baseline routing and compact worker feedback. | I8; deduplicated repair links, comparable baseline probe and no worker blame for environment/main failures. Changed-ADR fixture produces ≤10 lines; no full log in worker brief. |
| I11 | Diff-based review exemptions and Sol review policy. | I1; docs/mechanical verification and mixed-code fallback, explicit exemption receipts, risk/owner holds, all material HOLD findings and linked focused re-review. Gates remain required. |
| I12 | Review reuse and mechanical queue decisions. | I5/I11; exact-SHA reuse, proven identical replay, invalidation on changed preimage/conflict, main movement re-gate, full batch-tip receipts and fast-forward compare/update/recovery. No new publishing authority. |
| I13 | Four E-v named queries and evidence views. | I1 plus event fixtures; known frequency denominators, T-c-shaped review rounds, corrected/legacy fail classes, deduplicated retry costs including zero/unknown usage. Read-only query rollout can land before live routing. |
| I14 | Team pre-check/remote-authority integration. | Existing PR delivery contract and I3/I5; FieldWorks/liblcm/machine registrations stay disabled until approved. Local green never bypasses current PR CI/human merge rules. |
| I15a–c | Separate motif, drover and PanGloss registration migrations. | Each is a separate task after relevant adapters and I8–I13. Attach inventory, parity/drift report, real-placement gate evidence and rollback config. Drover's missing workflow source is its own prerequisite task; do not invent it during migration. |

Acceptance evidence for the implemented system must show: both required placements and all matrix variants gate locally without agents; unsupported work cannot become authoritative green; cold restore and T-cd boundary receipts; mechanical/docs/reused-review candidates landing without another review; new code receiving Sol review; a once-only flaky retry; environmental/baseline routing without task blame; useful compact changed-file failure feedback; silent-runner diagnosis; all review rounds and merge decisions reconstructible through E-v; all four named queries; and team PR authority preserved. Use fixture-based tests for policy and failure paths, then admitted real-platform pilots for runtime claims. A docs-only check of this spec is not a gate receipt for any implementation SHA.
