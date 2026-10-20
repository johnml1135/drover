# Audit

I read the requested local files only and made no changes. I counted each numbered finding once; repeated reports of the same issue share a row with their rounds listed. The tables contain **41 implementation/review findings and 41 plan-check findings**. One implementation-table row is a likely conformance-scope false positive, noted below. `p2d` and `p3e` reviews, and the `p3c` and `p3d` conformance passes, reported no findings.

## Implementation and conformance findings

“Unrated” means the artifact gave a verdict but no P-level severity. “Introduced” identifies where the gap arose; the scope-check false positive has no code defect to trace.

| Phase / round | Severity | One-line finding | Class | Introduced | Cheapest catcher and check |
|---|---|---|---|---|---|
| P2 R1 ([p2-review]) | P2 | Snapshot preparation loses commits reachable only from remote-tracking refs. | behaviour deleted without replacement | implementation | **Small:** prepare a dependency whose commit exists only under `refs/remotes/origin/*`, then check it out. |
| P2 R1 ([p2-review]) | P2 | Tool discovery can replace an explicit machine path with a shim that fails under the worker’s non-login environment. | other | implementation | **Small:** execute the discovered command under the actual worker environment; assert the configured path wins. |
| P2 conformance ([p2-conformance]) | Unrated, FAIL | An npm-specific sandbox default remains in generic Drover code. | scope | implementation | **Script:** scan production paths for prohibited repo, language, and tool names. |
| P2 conformance ([p2-conformance]) | Unrated, FAIL | A test probes for Node instead of testing the stable interface. | test defect | implementation | **Script:** flag runtime-probing patterns in tests. |
| P2 conformance ([p2-conformance]) | Unrated, FAIL | Repo `[run]` and `[scripts]` keys are read outside `repos.py`. | config | implementation | **Script:** search or parse for repo-key reads outside the accessor module. |
| P2 conformance ([p2-conformance]) | Unrated, FAIL | DrvFs, distro/registry, and UNC handling is spread outside `placement.py`. | scope | implementation | **Script:** check an allowlist for `/mnt`, registry, distro, and UNC logic. |
| P2 conformance ([p2-conformance]) | Unrated, FAIL | Hub dispatch retains a synchronous WSL gate path. | scope | implementation | **Script:** scan hub CI paths for direct WSL execution. |
| P2 conformance ([p2-conformance]) | Unrated, FAIL | The obsolete allowance headroom fallback remains reachable. | scope | implementation | **Script:** check call sites and owner-facing docs for the retired API. |
| P2 conformance ([p2-conformance]) | Unrated, FAIL | Dispatcher still treats `runs_dir` as optional on mandatory `Pipeline`. | scope | implementation | **Script:** enforce the required interface and flag optional attribute probing. |
| P2 conformance → P2 R2 → P2 core note ([p2-conformance], [p2b-review], [delete-core-review]) | P3 / Unrated | Tests still call private helpers or pin implementation text instead of exercising public behavior. | test defect | implementation | **Script:** AST-check tests for private helper calls and source-text assertions. |
| P2 R2 ([p2b-review]) | P2 | An unreachable task worktree makes versioned repo config disappear before recovery can run. | recovery or concurrency edge | implementation | **Small:** load a task with a missing persisted worktree and versioned scripts, then exercise recovery. |
| P2 R2 ([p2b-review]) | P2 | CI invokes bare `pwsh` even when discovery found it only at a configured machine path. | config | implementation | **Small:** use a fake `pwsh` off `PATH`; discover it, then execute the composed script. |
| P2 R2 ([p2b-review]) | P2 | Removing language filters also removed generic filtering for an explicitly requested platform. | behaviour deleted without replacement | implementation | **Small:** table-test a Windows task with a higher-priority WSL seat. |
| P2 R3 ([p2c-review]) | P2 | Setup reruns discard machine-file scripts and run settings needed during the migration fallback. | config | previous fix | **Small:** rerun setup with no versioned config and compare effective machine settings. |
| P2 core R1 ([delete-core-review]) | P1 | A configured writable root can overlap a read-only dependency snapshot. | scope | implementation | **Large:** model the access boundary; test `build_argv` with nested read and write roots. |
| P2 core R1 ([delete-core-review]) | P1 | The composed-command test feeds a WSL plan to the native Windows interpreter. | test defect | implementation | **Script:** run the native end-to-end test on Windows using the host platform. |
| P2 core R1 ([delete-core-review]) | P2 | An owner-selected script is lost when the exact-commit gate reloads config. | config | implementation | **Small:** answer the real unknown-script question, then build the exact-commit gate plan. |
| P2 core R1 ([delete-core-review]) | P2 | Docs CI uses the wrong kind on one path and skips the docs action on another. | config | implementation | **Small:** compare task and merge docs actions and `DROVER_CI_KIND` across platforms. |
| P2 core R1 ([delete-core-review]) | P1, reported fixed | WSL adoption scans still call the deleted `placement.wsl_argv`. | behaviour deleted without replacement | previous fix | **Script:** import/smoke-test the adoption scan after the placement deletion. |
| P2 core R1 ([delete-core-review]) | Unrated | A test still expects scripts in the intentionally slimmed machine example. | config | implementation | **Script:** run the example-config interface test after the fixture change. |
| P2 core R1 ([delete-core-review]) | Unrated | A legacy `commands` parameter and setup `gate_command` answer remain after the script contract replaced them. | scope | implementation | **Script:** flag unused parameters and retired config keys. |
| P2 core R1 ([delete-core-review]) | Unrated | A runner test asserts source text/module names rather than launcher behavior. | test defect | implementation | **Script:** flag tests that inspect implementation source. |
| P2 core R1 ([delete-core-review]) | Unrated | Tests mutate normalized config and bypass machine-layer provenance, masking reload bugs. | test defect | implementation | **Script:** flag `normalize`-then-mutate fixtures in reload tests. |
| P2 core R2 ([delete-final-review]) | P2 | `--filter=blob:none` is ignored for the local clone source, so snapshots still copy historical blobs; a WSL promisor source may also be unreachable from Windows Git. | other | previous fix | **Script:** use a real Git repo with a historical non-HEAD blob; assert it is missing while history operations work. |
| P2 core R2 ([delete-final-review]) | P2 | Valid Linux writable roots are rejected on native Windows because `/tmp/...` is not an absolute Windows path. | config | implementation | **Script:** run native Windows worker and gate tests with `/tmp/cache` configured. |
| P2 core R2 ([delete-final-review]) | P2 | Setup reruns reset effective landing remote/default branch to origin/main. | config | implementation | **Small:** round-trip an upstream/trunk machine configuration through setup twice. |
| P2 core R2 ([delete-final-review]) | P2 | Changed-files JSON contains Git’s quoted/escaped text instead of actual Unicode and newline paths. | other | implementation | **Script:** use real Git diffs containing `é.txt` and a newline filename; compare exact JSON paths. |
| P3 initial review ([ci-progress]) | Unrated | Queue/base movement during CI can leave the resumed landing service using a stale checkout. | recovery or concurrency edge | implementation | **Script:** change the queue/base during real CI, kill the caller, then resume. |
| P3 initial review ([ci-progress]) | Unrated | Late cancellation can leave a green result after the run was cancelled. | recovery or concurrency edge | implementation | **Large:** inject cancellation at runner completion and verify terminal status/result agreement. |
| P3 initial review ([ci-progress]) | Unrated | Long final records make runner scanning quadratic. | other | implementation | **Script:** benchmark adversarial long records and assert bounded/linear parsing behavior. |
| P3 R1 ([p3-review]) | P2 | CI consolidation drops Linux CPU/IO weights, P4 memory cap, timeout, and task-priority handoff. | behaviour deleted without replacement | implementation | **Small:** test priority and P4 limits through the CI interface. |
| P3 R1 ([p3-review]) | P2 | Configuring a docs script bypasses the generic `git diff --check` guard. | behaviour deleted without replacement | implementation | **Script:** run docs CI with conflict markers and a blank line at EOF. |
| P3 conformance → P3b conformance ([p3-conf], [p3b-conf]) | Unrated, FAIL | Tests create ledger tables outside the ledger schema/migration layer. | test defect | implementation | **Script:** reject `CREATE TABLE` outside `drover/ledger`; migrate an existing tasks table in the fixture. |
| P3 conformance ([p3-conf]) | Unrated, FAIL | The pass reports whole-tree platform logic as failure even though its brief limited failure to changed files; later passes treat that code as deferred. | scope | check scope; no code defect | **Script:** apply the changed-file filter mechanically and report remaining scheduled debt separately. |
| P3b conformance ([p3b-conf]) | Unrated, FAIL | An unused `consumes` field can carry cross-repo facts despite the generic-config boundary. | scope | implementation, Phase 2 loader | **Script:** flag unread config fields that permit policy/fact leakage. |
| P3b conformance ([p3b-conf]) | Unrated, FAIL | Pipeline, dispatcher, task access, hub, views, and wizard still read repo-config keys outside `repos.py` accessors. | config | implementation | **Script:** search or parse for repo-key reads outside `repos.py`. |
| P3 R2 ([p3b-review]) | P2 | A heartbeat/database-lock exception deletes a live runner’s adoption record and releases its slot while execution continues. | recovery or concurrency edge | implementation | **Large:** inject `lease_heartbeat` failure while a runner is live; verify request, ticket, and checkout remain adoptable. |
| P3 R2 ([p3b-review]) | P3 | Deleting `GatePlatform` also deletes docs-only regression coverage for mixed changes, symlinks, globs, exclusions, and Markdown hard breaks. | test defect | implementation | **Small:** map each deleted behavior to a named `CI.prepare`/`CI.run` test. |
| P3 R3 ([p3c-review]) | P2 | A confirmed-dead runner is repeatedly adopted, so landing remains blocked on every resume. | recovery or concurrency edge | previous fix | **Large:** confirm runner/children stopped, resume twice, and assert release plus successful retry. |
| P3 R4 ([p3d-review]) | P2 | The landing CLI creates a new checkout and never resumes interrupted CI, causing duplicate work and orphaned state. | recovery or concurrency edge | brief | **Owner:** decide whether the CLI is a supported driver; then test single execution on retry or delete that path. |
| P3 fix R3 ([p3-fix3]) | Unrated, fix-brief finding | A test calling `git hash-object -w --stdin` without input hangs on inherited stdin. | test defect | previous fix | **Script:** run the test with subprocesses defaulting to `DEVNULL` unless explicit input is supplied. |

## Plan-check findings

All rows below were marked **REVISE**. In each case, the defect was in the plan being checked.

### Workflow design, round 1 ([workflow-check])

| # | Severity | One-line finding | Class | Introduced | Cheapest catcher and specific check |
|---|---|---|---|---|---|
| 1 | REVISE | Tasks’ write sets omit runtime files while dependency 7→6 conflicts with “build now”; protected-module boundaries are unclear. | scope | plan | **Small:** inspect the task dependency graph and write sets for protected paths. |
| 2 | REVISE | Task 7 depends on an unowned template/actions; wildcard paths obscure the six report destinations. | plan precision | plan | **Script:** resolve every referenced template, action, script, and report destination; reject wildcards. |
| 3 | REVISE | Loader return shape, resolved SHA, artifacts/hashes, serialization, errors, symlinks, fallback, and override rejection are unspecified. | config | plan | **Large:** review the loader as a complete data/provenance contract. |
| 4 | REVISE | Codec extraction lacks exact existing/new signatures, exception dependency, protections, and full write set. | plan precision | plan | **Small:** compare the plan’s signatures and write set against the existing codec/schema/tests. |
| 5 | REVISE | Policy validation omits keys, defaults, types, identifiers, tier/action requirements, stage ordering, and gates. | config | plan | **Owner:** approve the policy schema and required gates per level. |
| 6 | REVISE | Selector output, floors, conflicts, abstention, unavailable levels, and promotion behavior lack a decision contract. | config | plan | **Owner:** decide the selection and promotion protocol. |
| 7 | REVISE | Verification lacks exact fixtures, assertions, red/pass commands, outcome schema, criterion/test map, and bounded tasks. | plan precision | plan | **Script:** require each criterion to name a test, expected failure, and executable command. |
| 8 | REVISE | Citations and line anchors do not match the verified HEAD. | plan precision | plan | **Script:** validate paths, symbols, and line anchors against the pinned checkout. |
| 9 | REVISE | Deferred tasks lack landed prerequisites, migrations/replay rules, and rollback/rehearsal contracts. | scope | plan | **Large:** cross-check deferred task contracts against prerequisite phases and recovery. |

### Workflow T1, round 1 ([workflow-t1-check])

| # | Severity | One-line finding | Class | Introduced | Cheapest catcher and specific check |
|---|---|---|---|---|---|
| 1 | REVISE | Plan base and dependencies do not reflect Phase 3b’s landed config/overlay rules. | scope | plan | **Script:** compare the declared base and config paths against the prerequisite commit. |
| 2 | REVISE | Artifacts, fixtures, scripts, risks, and rollback instructions still use the superseded root-config layout. | config | plan | **Script:** scan all task paths and examples for stale config/overlay names. |
| 3 | REVISE | Definition validation permits missing planners and conditional required gates at L3–L6. | config | plan | **Small:** table-test level definitions against mandatory planner/gate rules. |
| 4 | REVISE | Selection schemas omit checkpoint/predicate evidence, default behavior, conflict paths, judgment matching, and promotion transitions. | config | plan | **Owner:** resolve precedence and state-transition semantics. |
| 5 | REVISE | Outcomes prose leaves public counters, rates, denominators, exclusions, histogram, and numeric types undefined. | plan precision | plan | **Small:** review the output schema field-by-field against the intended report. |
| 6 | REVISE | Many verification tasks lack complete fixtures, exact red assertions, expected failures, commands, and named tests. | plan precision | plan | **Script:** require behavioral acceptance criteria to name fixtures, assertions, and commands. |
| 7 | REVISE | Binary subprocess environment/timeout behavior and one citation are incomplete. | plan precision | plan | **Script:** validate cited anchors and require explicit subprocess contract fields. |
| 8 | REVISE | Prior-check dispositions and the 3b supersession note are incomplete. | plan precision | plan | **Small:** trace every prior erratum to a correction or explicit deferral. |

### Waiting plan, round 1 ([waiting-plan-check-v1])

| # | Severity | One-line finding | Class | Introduced | Cheapest catcher and specific check |
|---|---|---|---|---|---|
| 1 | REVISE | Citations point to stale lines/anchors on the stated base. | plan precision | plan | **Script:** validate cited symbols and line references at the pinned SHA. |
| 2 | REVISE | Phase 3 CI recovery and repo-accessor behavior are not preserved explicitly. | recovery or concurrency edge | plan | **Script:** require regression coverage for `CI.resume`, `CIInterrupted`, and script-answer reload. |
| 3 | REVISE | The full-suite red interval, replan behavior, fixture timing, and Tasks 8–9 dependency are underspecified. | scope | plan | **Small:** walk the dependency graph and identify tasks that cannot run independently. |
| 4 | REVISE | Tasks lack exact write sets, signatures, changed paths, red commands, and expected failures. | plan precision | plan | **Script:** require each task to list files, signatures, and executable red/pass commands. |
| 5 | REVISE | Automatic question/comment requeue paths can bypass remaining waits or rejected answers. | recovery or concurrency edge | plan | **Script:** test invalid/non-owner answers, unrelated comments, and one-of-many releases. |
| 6 | REVISE | Wait resolver events, payloads, kinds, recursion, release, and ownership protocols are incomplete. | plan precision | plan | **Large:** model each wait kind through registration, resolution, replay, and release. |
| 7 | REVISE | Baseline behaviors are not mapped to replacement interface tests before deletion. | test defect | plan | **Small:** map each named characterization case to its replacement test and task. |
| 8 | REVISE | Requirement/test mapping omits concrete release, retry, reset, and configuration assertions. | plan precision | plan | **Small:** audit every requirement against a named test and asserted value. |
| 9 | REVISE | Pin/retry/planner choices and migration transaction/rollback/rehearsal behavior remain open. | recovery or concurrency edge | plan | **Owner:** settle policy choices and require migration rollback/idempotence proofs. |

### Waiting plan, round 2 ([waiting-plan-check])

| # | Severity | One-line finding | Class | Introduced | Cheapest catcher and specific check |
|---|---|---|---|---|---|
| 1 | REVISE | Citations, test path prefixes, and baseline HEAD references remain inconsistent. | plan precision | plan | **Script:** validate all paths and symbols against the stated HEAD. |
| 2 | REVISE | Fixtures, instructions, smoke commands, and task dependencies still assume pre-3b config paths. | scope | plan | **Script:** scan task fixtures and commands for superseded paths. |
| 3 | REVISE | Declared integration sequencing conflicts with whole-suite test groups and leaves broken-import intervals undefined. | scope | plan | **Small:** simulate task order and the tests runnable after each task. |
| 4 | REVISE | Write sets omit callers, fields, and exact function edits/signatures. | plan precision | plan | **Script:** compare each task’s changed-symbol inventory to its read/write set. |
| 5 | REVISE | Characterization lacks explicit preservation mappings and named release tests per wait kind. | test defect | plan | **Small:** require one preserved-behavior assertion per existing behavior/wait kind. |
| 6 | REVISE | Wait-kind payloads, handler selection, release/clear, shared-subject retry, and deadlines remain ambiguous. | recovery or concurrency edge | plan | **Large:** model resolver, retry, and release transitions together. |
| 7 | REVISE | Migration rules for legacy events, malformed state, rollback, idempotence, and release lack executable assertions. | plan precision | plan | **Script:** require a migration rehearsal with named assertions for replay and rollback. |

### Config plan, round 1 ([config-check])

| # | Severity | One-line finding | Class | Introduced | Cheapest catcher and specific check |
|---|---|---|---|---|---|
| 1 | REVISE | Task order postpones matching tests and fixture migration while claiming full-suite guarantees. | scope | plan | **Script:** validate task dependencies, fixture ownership, and suite command paths. |
| 2 | REVISE | Tasks omit exact failing commands, expected failures, signatures, and concrete file substitutions. | plan precision | plan | **Script:** require per-task red commands and explicit changed paths/signatures. |
| 3 | REVISE | Wizard adoption/fallback rules do not define the marker, timing, retained raw values, or omitted versus explicit `push=False`. | config | plan | **Owner:** decide adoption and serialization behavior for these inputs. |
| 4 | REVISE | Rerun characterization misses canonical remote/default-branch and brief/review/land-policy preservation. | test defect | plan | **Small:** compare machine-file behavior across two setup reruns. |
| 5 | REVISE | Invalid TOML, machine-only keys, types, paths, fallback, and diagnostics lack rejection tests. | config | plan | **Script:** run a table of malformed configs and assert named diagnostics/no fallback. |
| 6 | REVISE | Provenance JSON shape, attribution, cache operations, and dotted env keys lack a data contract. | config | plan | **Large:** review source attribution and cache replacement as one API contract. |
| 7 | REVISE | Transient script-answer precedence across exact-commit reloads and fallback config is unspecified. | config | plan | **Large:** trace owner authorization and script provenance through multiple revisions. |
| 8 | REVISE | Overlay-removal tests still read removed exports and do not prove checkout config is ignored by the machine loader. | scope | plan | **Script:** assert obsolete exports are absent and checkout config cannot enter machine loading. |

## Counts

Counts below are by table row, with repeated identical findings collapsed. The raw total includes one scope-check false positive (the P3 whole-tree placement report); excluding it reduces the total to 81 and scope to 17.

| Defect class | Count |
|---|---:|
| Behaviour deleted without replacement | 5 |
| Recovery or concurrency edge | 10 |
| Test defect | 11 |
| Plan precision (paths, lines, commands) | 15 |
| Scope | 18 |
| Config | 19 |
| Other | 4 |
| **Total** | **82** |

| Cheapest catcher | Count |
|---|---:|
| Script check or regression test | 44 |
| Small model | 22 |
| Large model | 10 |
| Owner | 6 |
| **Total** | **82** |

The `p2d` and `p3e` reviews and the `p3c`/`p3d` conformance passes close out their rounds without findings. The later P3 conformance passes also treat much of the platform code listed in `p3-conf` as scheduled debt, rather than a Phase 3 failure.

## Agent runs and the real split

The documents are work-product reports, not invocation logs: they generally omit run IDs, actual model settings, and per-run effort. The closest defensible roster is therefore **artifact counts with role assignments from [PLAN.md]**, not a verified count of model calls.

| Work stream | Run-shaped artifacts | Model/effort recorded or assigned | Class |
|---|---|---|---|
| Phase 2 implementation and fixes | Delete pass plus three fix briefs/reports: 4 | PLAN assigns Codex GPT-6.1-Sol; per-run effort is absent. Large is the intended job tier, not a confirmed actual setting. | Large, inferred |
| Phase 2 reviews | `p2`, `p2b`, `p2c`, `p2d`, core, final: 6 | PLAN assigns Sonnet reviews at medium tier; individual invocation settings are absent. | Medium |
| Phase 2 conformance | `p2-conformance`: 1 | PLAN assigns Haiku; exact effort is absent. | Small |
| Phase 3 implementation and fixes | CI module plus four fix rounds: 5 | Same Codex implementer role; per-run effort is absent. | Large, inferred |
| Phase 3 reviews | Initial CI review plus `p3` through `p3e`: 6 | PLAN assigns Sonnet reviews at medium tier; individual invocation settings are absent. | Medium |
| Phase 3 conformance | `p3`, `p3b`, `p3c`, `p3d` conf passes: 4 | PLAN assigns Haiku; exact effort is absent. | Small |
| 5b/5c/5d research and plans | Router, ledger, authority: 3 | PLAN explicitly assigns Sol medium. | Medium |
| Workflow, Waiting, and config plans/checks | Five plan/check pairs: 10 | These artifacts do not record a model or effort. Workflow design recommends large planning and medium checking, but that is a design recommendation, not run telemetry. | Unknown |

That gives a **work-product proxy** of 9 large-intended, 15 medium, and 5 small artifacts, plus 10 unclassified planning artifacts. It does not yield today’s 70/20/10 split: [morning.md] says that ratio measures *orchestration decisions*, while these files count task outputs and mostly lack decision-level telemetry. Script tests and suites are recorded in several reports, but their invocation counts and decision denominator are not consistent enough to add them to that ratio. Phase 6’s Opus review is planned in PLAN.md; there is no completed run artifact for it.

## Five changes that would have prevented the most review rounds

1. **Require a behavior-to-test replacement map before deletion.** For every removed path, name the live invariant and its replacement interface test. This addresses lost routing, repo-config, docs, resource-control, and docs-only behavior.

2. **Use one runner recovery matrix before CI implementation.** Inject heartbeat/DB-lock failure while live, confirmed runner death, cancellation, queue/base movement, child-process termination, and repeated resume. Include every supported driver, or settle and remove duplicate drivers before coding. This targets the P3b–P3d sequence.

3. **Make config provenance a single end-to-end contract.** Test machine fallback, committed config at an exact revision, missing worktree recovery, owner script answers, setup reruns, and preserved remote/default branch in one interface matrix. This would have caught most P2 config rounds.

4. **Require Windows and WSL composition checks before review.** Run the composed command on each native host and bridge path, including `/tmp` roots, pwsh discovery, Unicode paths, and stdin handling. Several findings were specifically platform failures or tests that never exercised the named command on Windows.

5. **Add a plan linter and acceptance matrix before plan review.** Check references, citations, write/read sets, task dependencies, exact red/pass commands, config paths, and requirement-to-test mappings. This directly targets the repeated workflow, Waiting, and config `REVISE` rounds.

[PLAN.md]: scratchpad/sol/PLAN.md
[morning.md]: scratchpad/morning.md
[p2-review]: scratchpad/sol/p2-review.md
[p2-conformance]: scratchpad/sol/p2-conformance.md
[p2b-review]: scratchpad/sol/p2b-review.md
[p2c-review]: scratchpad/sol/p2c-review.md
[p2d-review]: scratchpad/sol/p2d-review.md
[delete-core-review]: scratchpad/sol/delete-core-review.md
[delete-final-review]: scratchpad/sol/delete-final-review.md
[p3-review]: scratchpad/sol/p3-review.md
[p3-conf]: scratchpad/sol/p3-conf.md
[p3b-conf]: scratchpad/sol/p3b-conf.md
[p3b-review]: scratchpad/sol/p3b-review.md
[p3c-review]: scratchpad/sol/p3c-review.md
[p3d-review]: scratchpad/sol/p3d-review.md
[p3e-review]: scratchpad/sol/p3e-review.md
[ci-progress]: scratchpad/sol/ci-progress.md
[p3-fix3]: scratchpad/sol/p3-fix3.md
[workflow-check]: scratchpad/sol/workflow-check.md
[workflow-t1-check]: scratchpad/sol/workflow-t1-check.md
[waiting-plan-check-v1]: scratchpad/sol/waiting-plan-check-v1.md
[waiting-plan-check]: scratchpad/sol/waiting-plan-check.md
[config-check]: scratchpad/sol/config-check.md
