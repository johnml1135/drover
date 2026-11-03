# Drover Rearchitecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Drover's shared machinery owners (process, git, listing, atomic files, cleanup stage, cleanup authority, run output) without changing what cleanup may delete.

**Architecture:** Slices land in the design's order on `arch/rearchitecture`. Each slice ends with the old code deleted and its guards true. Characterisation comes first, then the live listing problem, then the operating-system edge, then the stage run, then the cleanup re-cut by concept: evidence knows, authority rules, removal acts, stages ask.

**Tech Stack:** Python 3 stdlib, `unittest`, git, `tests/fakeci.py` `FakeCommands`, AST guards in `tests/test_guards.py`, `tests/test_architecture.py` and the new `tests/test_seams.py`.

**Spec:** docs/superpowers/specs/2026-11-02-rearchitecture-design.md

Section numbers below are that design's. Appendix B overrides. A task's requirements include every section it cites, and this plan does not repeat its tables. Plan path in the repo: `docs/superpowers/plans/2026-11-02-rearchitecture.md`.

## Global Constraints

Every task's requirements include this section. The controller gives it, with Conventions, to every implementer and reviewer.

**Where**
- Work only in `H:\repos\drover.worktrees\rearchitecture`, branch `arch/rearchitecture`, based on 92ce946.
- Nothing may run `git checkout` or `git switch` in that worktree or in `H:\repos\drover.worktrees\cleanup-stage4`. Reviewers read with `git show`, `git diff` and `git log`.
- Nothing may touch `C:\Users\johnm\.drover`.
- Build with fresh Opus agents: every implementer, fix round, task reviewer and checkpoint reviewer is a new Opus subagent, whatever `subagent-driven-development`'s model table would pick.

**Rulings, verbatim where they bind**
- **Ruling 3, the decision trace.** "The decision trace may change only in slices 1 and 5, only towards keeping more or adding a report row, with each changed row listed in the slice's report and read by its fresh Opus reviewer. Any change towards more deletion, in any slice, stops that slice; it does not re-record. Slice 6 may not change it. 'Stops and asks' means the implementer reports BLOCKED to the session lead, who rules or goes to the owner."
- **More deletion.** Any change towards more deletion stops the slice and reports BLOCKED. There is no exception, and no re-record.
- **Ruling 6.** "Subagents never run a live cleanup, dry or applied, and never touch the owner's state directory or lock. They may take read-only timings on /mnt/h and may start a test stage through real `wsl.exe` in a temp tree. Live dry runs, applies, deploys, merges and pushes are the owner's. 'Landed' means committed on `arch/rearchitecture` with tests and review passed. Nothing is deployed during this work."
- **So:** no live cleanup, no deploy, no merge, no push by subagents.
- **Ruling 13.** "A file reader must never be the authority that something may go. Real `git worktree list --porcelain -z` stays the authority for selection and for repo-level failure: it runs once per repo per stage run, across all configured repos, on a long timeout set from the measurements, and any failure leaves that repo wholly unknown as today (nothing in it removed, no stamp). `git_worktrees` reads admin files only for the fresh proof at removal, and is compared with that stage's listing, including `locked`, branch and resolved HEAD, for the target's repo and for nesting in every repo. It can only refuse: any difference, unreadable field or missing file is keep. It never adds a registration git did not list, and a registration git listed that it cannot read is unknown."
- Ruling 14 narrows ruling 13 for non-target rows: compare the pointer and a readable `locked` and `commondir`, not branch, lock text or tip.
- Ruling 5: if review changes the four commits up to 92ce946, stop and report BLOCKED: the trace is re-recorded.
- Ruling 15: the frozen `hub/reload_protocol` and `hub/runtime` import no `durable`, `process` or `git_client`, whatever section 5 says. Ruling 16: the hand-run recorder plus the default-tier replay meet section 10's real-git check.

**Tests**
- Test command, from the worktree: `DROVER_TESTS=integration H:/repos/drover.deploy/hub/.venv/Scripts/python.exe -m unittest ...`
- TDD: red first in every production task. A test that passes before the code changes is not a red.
- New tests go in the default tier, with fakes at the design's seams. No new real-process test.
- `INTEGRATION_CEILING` 209 and `IMPURE_CEILING` 65 are never raised. Both guards assert equality, so a test converted lowers the constant in the same commit. A module leaving `tests.IMPURE` in `tests/__init__.py` lowers `IMPURE_CEILING` in that commit.
- Replace, don't add parallel tests. When a test moves or dies, the task says so, and the old one goes in the same commit.
- A task verifies only its affected test modules plus `tests.test_seams`, `tests.test_guards` and `tests.test_architecture`. The full suite runs once at each checkpoint, not per task.

**Guards, every slice**
- `tests/architecture_import_allowlist.json` gains no rows: 1,833 rows and 7,353 bindings are the maximum. Rows may be renamed one for one, or deleted.
- `MAX_LINES` stays 500. A touched file near the cap must shrink: `pipeline` 491, `ci` 494, `config` 493, `disk_paths` 493, `disk_worktrees` 497, `disk` 489, `resources` 492.
- `OVER` stays or falls. `hub/runtime` and `hub/reload_protocol` keep their local code (ruling 15): neither may grow, and a pin is lowered only in a commit that shrinks its file.
- `tests/seam_exceptions.json` is asserted equal, so a row goes in the commit that makes it false.

**Commit rule.** One declarative sentence, blank line, then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage named files only.

**Memory.** The machine is short of memory. Never run two suites at once. Checkpoint runs use `-j 4`.

## Review Focus

Inputs the design implies and no design-listed test covers, most likely first. Each has its test in the owning task.

1. A repo with no linked worktrees, so no `worktrees/` directory, or a fresh clone: `prove` agrees and refuses nothing, because most small repos look like this. Task 9.
2. A worktree path with non-ASCII characters, or a `gitdir` pointer ending in CRLF, or bytes that are not valid UTF-8: the pointer compares equal to git's row when it is the same place, and an undecodable field refuses rather than crashes. A false refusal here would stop all cleanup on the machine. Task 9.
3. A drive so full that the write fails, which is when cleanup runs: `durable` leaves the target whole, removes the temp, retries nothing and raises the `OSError`. Task 11.
4. A small `worker_timeout_s`, such as the 11 s in `tests/placement_trace.py`: `mutation_cap()` falls to 0.44 s, so `can_mutate()` and the step timeouts must still give a `failed` journal row or a `removal_deadline` keep, never a removal left between `planned` and `completed` without one. Task 30 and Task 43.
5. One repo configured by two spellings (a trailing slash, a different case on Windows, `H:\` and `/mnt/h`): `Listings` lists it once, and a stage's own `removed` under one spelling is honoured under the other, so the stage's next removal is not refused. Task 8.

## Conventions

**Shell preamble.** Every command below assumes this, pasted once per shell call in Git Bash:

```bash
cd H:/repos/drover.worktrees/rearchitecture; PY=H:/repos/drover.deploy/hub/.venv/Scripts/python.exe
mods(){ (cd tests && grep -lE "$1" test_*.py | sed 's/\.py$//;s/^/tests./'); }
changed(){ { git diff --name-only --diff-filter=d HEAD -- 'tests/test_*.py'; git ls-files -o --exclude-standard -- 'tests/test_*.py'; } | sed 's/\.py$//;s#/#.#'; }
GUARDS="tests.test_seams tests.test_guards tests.test_architecture"
```

`tests.test_seams` exists from Task 3. Tasks 1 and 2 run no verify command.

**Verify (the owner's test command).**

```bash
DROVER_TESTS=integration $PY -m unittest <modules> $GUARDS
```

Passes on a final `OK`, skips allowed.

**Purity line.** The verify command cannot check purity: with `DROVER_TESTS=integration` set, `tests/__init__.py` installs no purity guard and defines no `tests.IMPURE`, and `DefaultTierPurity` is skipped. So a new test that spawns git would pass it. Every task that adds or changes a test module also runs that module in the default tier. Migration tasks (15 to 19, 23 to 28) run it over all of `$M`: an unchanged test can still patch a function production stopped calling, and then run real git without any failure under the verify command.

```bash
env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
```

`changed` names every test module this commit adds or changes, so it runs before the commit. Passes on `OK`. A failure reading `default tier must not use subprocess` means the test is real-process: fake it. `tests.test_placement` and other `IMPURE` modules run here only if they were changed.

**Red for a move.** A task that only moves code has no behaviour to fail, so its red is a guard. It adds the old qualified name to `tests/architecture_removed_symbols.json` under `qualified_names`, or deletes the old `tests/seam_exceptions.json` row. It also adds a test importing from the new home. The failure must name the old symbol, or show `ImportError` for the new one.

**Red for a migration.** It deletes the module's R1 row and restates the module's fake as `FakeCommands.patch(process)` with `process_options(...)`. `tests.test_seams` must fail naming the module, and the restated test must fail with `unused command expectations`, or with a real-process refusal.

**Trace check at every slice exit.** `SLICE_BASE` is the commit before the slice's first task, kept in the ledger.
- Slices 2, 3a, 3b, 4, 6a, 6b, 6c, 7: `git diff --exit-code $SLICE_BASE -- tests/disk_decision_trace.json` must print nothing.
- Slices 1 and 5: `git show $SLICE_BASE:tests/disk_decision_trace.json > "$TEMP/old-trace.json" && $PY -m tests.disk_trace --compare "$TEMP/old-trace.json" tests/disk_decision_trace.json`. It must print no `towards_deletion` row. Every other changed row goes in the slice report.

**Reports.** Committed, read by reviewers, stored under `docs/superpowers/reviews/`:
- `2026-11-02-rearchitecture-slice-<id>.md`: `SLICE_BASE`, changed trace rows, command-trace changes, handler lists, what a checkpoint must know.
- `2026-11-02-rearchitecture-test-times.md`: baseline and each checkpoint's times.
- `2026-11-02-rearchitecture-checkpoint-<id>.md`: three reviews and their fixes.

**Task loop.** Per `subagent-driven-development`: fresh Opus implementer, then a fresh Opus task reviewer on the review package. The task reviewer also confirms that the red was real, that nothing beyond the task landed, and that Global Constraints hold. Plain integer task numbers keep `task-brief` and the ledger working. Checkpoint tasks have no implementer until they have findings.

**Checkpoint protocol.** Each checkpoint task runs this once.
1. With nothing else running, the controller runs both tiers and saves the output in the checkpoint report:
   ```bash
   $PY scripts/test.py -j 4 --durations 10 > "$TEMP/cp-default.txt" 2>&1; $PY scripts/test.py -j 4 --tier integration > "$TEMP/cp-integration.txt" 2>&1
   ```
   Both must end `0 failed modules`. `scripts/test.py` is used rather than the verify command because it is the only route that prints seconds per module and runs the default tier with purity on; the two tiers are disjoint and together cover what the verify command runs.
2. It appends the times to the test-times report, by the method in Task 1.
3. It starts three separate fresh Opus reviewers, read-only: no edits, no commits, no test runs, no `git checkout` or `switch`. They may read, grep and use `git show`, `git diff`, `git log`. Each gets the spec, this plan, `LAST_CP..HEAD` as a commit range, and the slice reports since `LAST_CP`.
4. Findings go to a fresh Opus implementer, red first, then a scoped re-review by a fresh reviewer of the same role. All are fixed before the next slice starts. A finding the fixer disputes goes to the session lead, and is not dropped.
5. Its last step commits the checkpoint report and the test-times row, after any fixes. That commit is `LAST_CP` for the next checkpoint.

| Reviewer | Also gets | Must answer | Passes when |
|---|---|---|---|
| Test completeness | Spec sections for the slices in range, Review Focus, the list of tests added, moved and deleted | For every interface, invariant, error mode and table row in those sections, and every Review Focus line: the test that fails if it breaks, by name | Every row names a test, and each named test could fail for that reason. "Covered indirectly" is a finding unless the path is traced |
| Scope | Section 10's landing rows and removed-symbol lists, `tests/seam_exceptions.json`, the allowlist diff | Anything beyond the design; anything in the design for these slices skipped; old code left behind, by grep for each deleted name; any temporary bridge still alive | No unlisted change, each landing row true, allowlist not grown, ceilings exact, trace check clean |
| Efficiency | Both checkpoint outputs, the test-times report, the census re-run | Test time against target; duplicate tests; tests that could be cheaper, and at which seam; production cost on the hot paths this checkpoint names | Target met, or a stated reason accepted by the lead; no duplicate pair left; no hot path costing more than the design states |

Severity: **Critical** and **Important** must be fixed. **Minor** goes in the checkpoint report, and is fixed only if it is a one-line test or guard change.

## Test Time

**Method.** Task 1 records, per module, the default-tier seconds from `scripts/test.py -j 4` and the real spawn count from a census. Seconds vary with load, so spawn count is the stable measure, and seconds are compared only at the same `-j` with no other suite running.

**Where the design lets tests get faster.**

| Seam | Slice | Replaces | First candidates |
|---|---|---|---|
| `durable.time` patched | 2 | Real retry sleeps | `test_disk_state`, `test_runner_status_write`, `test_compaction_hold` |
| `FakeCommands.patch(process)`, one fake for every spawn | 3a, 3b | `run=` and `runner=` fakes (82 and 23 arguments), real commands | `test_disk_trim`, `test_placement`, `test_resources`, `test_usage`, `test_disk_boundaries` |
| Process-level fakes of `git_client` | 4 | Real git spawns where git is setup, not the point | `test_origin_sync`, `test_task_access`, `test_task_views`, `test_hub_runtime_paths`, `test_disk_branch`, `test_disk_policy`, `test_disk_ownership`, `test_disk_caches` |
| `git=` object on `origin`, `pipeline.Repository` facts | 4 | Real repos in landing tests | `test_landing_repository` default-tier tests |
| Recorded `registration_trace.json` replay | 1 | Real git in listing tests | `test_disk_worktrees`, `test_disk_registration_timeouts` |
| `StageClock(clock=)`, a list for stdout | 5 | `disk_budget._clock` patches (4 sites), worker spawns | `test_disk_settled_deadline`, `test_disk_budget*`, `test_disk_temp_budget` |
| `StageEvidence` fakes, `MemoryCleanupEvidence` | 6 | Real worktrees in authority tests | `test_disk_fix7`, `test_disk_fix8`, `test_disk_fixes`, `test_disk_report`, `test_disk_worktrees` |

**Targets.** Baseline B comes from Task 1; [I] means judgement. A miss is not hidden: the efficiency reviewer rules on it with the lead. A candidate above that cannot convert stays, with its reason in the slice report, and the numbers below move with the lead's agreement.

| Checkpoint | Default-tier seconds, affected modules | Real spawns, affected modules | `tests.IMPURE` | Integration tests | New test modules |
|---|---|---|---|---|---|
| After slice 0 | B recorded; new tests under 15 s together | B recorded | 65 | 209 | `test_seams` under 5 s, trace test under 10 s |
| After slice 3b | at most B | at most 90% of B | at most 63 | at most 209 | Each under 2 s |
| After slice 4 | at most 90% of B | at most 70% of B | at most 59 | at most 205 | Each under 2 s |
| After slice 6c | at most 80% of B, disk modules | at most 60% of B | at most 55 | at most 200 | Each under 3 s |
| End | Sum of all modules at most 90% of the full-suite baseline | at most 60% of B | at most 55 | at most 200 | Same |

## File Map

| File | Responsibility | Task |
|---|---|---|
| `tests/test_seams.py`, `tests/seam_exceptions.json` | Guards R1 to R9 | 3, grown by later tasks |
| `tests/registration_trace.py`, `.json`, `tests/test_registration_trace.py` | Hand-recorded real git over the damage fixture; default-tier replay | 4 |
| `tests/disk_trace.py`, `tests/disk_decision_trace.json`, `tests/test_disk_decision_trace.py` | Decision trace harness, record, test | 5, 6 |
| `drover/git_worktrees.py`, `tests/test_git_worktrees.py` | Stage listing, fresh proof | 8, 9 |
| `tests/test_disk_fresh_proof.py` | Removal against the proof, through the trace harness | 10 |
| `drover/durable.py`, `tests/test_durable.py` | Atomic files | 11 |
| `drover/process.py`, `tests/test_process.py` | Short-lived commands | 14 |
| `drover/git_client.py`, `tests/test_git_client.py` | Git questions | 22 |
| `drover/disk_stage.py`, `tests/test_disk_stage.py` | Request, clock, rows | 30, 31 |
| `drover/disk_evidence.py`, `tests/test_disk_evidence.py` | One stage run's evidence | 33, 38, 39 |
| `drover/disk_authority.py`, `tests/test_disk_authority.py` | Rules | 40 to 42 |
| `drover/disk_removal.py` | Journalled removal; tests stay in `tests/test_disk_removal.py` | 43, 44 |
| `drover/run_output.py`, `tests/test_run_output.py` | Run output record | 47 |

Not in this plan: **slice 8, the conditional coordinator reads.** It is not built. O4 is an owner check (Task 49, check 8).

---

## Slice 0: characterisation

No production code changes in this slice.

### Task 1: Confirm the base and record the test-time baseline

**Files:**
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-test-times.md`

**Interfaces:**
- Consumes: the full run on 92ce946 in this worktree, already under way when the plan was written.
- Produces: B for every later target.

- [ ] **Step 1: Read the run.** Ask the lead for its output. Any failure: report BLOCKED. Do not re-run it. Ruling 5 needs the suite to pass on 92ce946 before slice 0.
- [ ] **Step 2: Default-tier seconds.** If that run went through `scripts/test.py` in the default tier, take its per-module seconds. Otherwise, with nothing else running:
  ```bash
  $PY scripts/test.py -j 4 --durations 10 > "$TEMP/base-default.txt" 2>&1
  ```
  Expected: `0 failed modules`. This is the one extra default-tier run the plan allows.
- [ ] **Step 3: Spawn census.** Keep this script out of the repo, in `$TEMP/census.py`, run with `DROVER_TESTS` unset: it imports `tests`, wraps `subprocess.Popen.__init__` and `os.system` with a counter keyed by `tests._caller_module()`, runs `tests.tiers.TierLoader` over one module at a time, and prints `module spawns`. Run it for every module in `tests.IMPURE`, and for each of `mods 'placement|disk_|git'`.
  Expected: a count per module, and 0 for modules outside `IMPURE`.
- [ ] **Step 4: Write the report.** One table: module, tests, seconds, spawns, tier, whether it is in `IMPURE`. Below it, the sum over all modules, `-j`, load at the time, and the suite's total. Mark the top 15 by spawns as conversion candidates, with a note on whether the spawn is setup or the point of the test.
- [ ] **Step 5: Commit** `docs/superpowers/reviews/2026-11-02-rearchitecture-test-times.md`: "The default-tier baseline for the rearchitecture records seconds and real spawns per test module."

### Task 2: Survey the live repos read-only

**Files:**
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-survey.md`

**Interfaces:**
- Produces: the O3 answer that Task 9's `parse` relies on; the count of repos ruling 9 would refuse.

Ruling 6 applies. No cleanup runs, and `.drover` is not read, so the repo list is the directories under `/mnt/h/repos`.

- [ ] **Step 1: Read admin directories.** A script kept out of the repo, run in WSL, reads `gitdir`, `locked` and `commondir` of each `/mnt/h/repos/*/.git/worktrees/*`, serially, read-only. Count: admin directories, empty or unreadable pointers, bad `commondir`, and any `<common>/reftable`.
- [ ] **Step 2: Compare spellings.** For drover, PanGloss, motif and one small repo: `git worktree list --porcelain -z` from WSL, then the same with native Windows git. Record each row's spelling against the admin pointer, drive-letter pointers above all. Run them one at a time.
- [ ] **Step 3: Settle O3.** State what WSL git does with `H:/...` pointers. If it contradicts section 16's O3 account, report BLOCKED: it returns to the session lead.
- [ ] **Step 4: Commit** the survey with counts and no worktree contents: "The read-only survey records the live admin directories and settles how WSL git spells drive-letter pointers."

### Task 3: Guards R1 to R6

**Files:**
- Create: `tests/test_seams.py`, `tests/seam_exceptions.json`

**Interfaces:**
- Consumes: `call_name`, `debt`, `trees`, `compare` from `tests/test_architecture.py`.
- Produces: rows of `{"rule": "R1", "module": "drover/ci.py", "where": "<function or <module>>", "count": 1, "reason": "..."}`. They are keyed by module and function, not by line, so unrelated edits do not churn them. `rule_violations(rule, sources) -> list[row]`, run over real trees or over planted source strings. Later tasks add R7, R8, R9 and direction rules here.

- [ ] **Step 1: Failing tests**, in `tests/test_seams.py`, default tier:
  ```python
  class Rules(unittest.TestCase):                # one planted source per rule
      def test_r1_sees_import_dunder_import_and_placement_subprocess(self):
          for text in ('import subprocess', "__import__('subprocess')", 'placement.subprocess.run([])'):
              self.assertEqual(rule_violations('R1', {'drover/x.py': text})[0]['module'], 'drover/x.py')
      def test_r2_sees_replace_rename_and_one_argument_path_replace(self): ...
      def test_r2_ignores_two_argument_str_replace(self): ...
      def test_r3_sees_git_argv_outside_placement_git(self): ...
      def test_r3_requires_unknown_beside_answer_no(self): ...
      def test_r4_sees_rmtree_unlink_rmdir_in_listed_modules(self): ...
      def test_r5_direction_rule_goes_live_with_its_module(self): ...   # absent module: no finding
      def test_r6_counts_sibling_imports_inside_functions(self): ...
  class Seams(unittest.TestCase):                # the real tree against the manifest
      def test_rows_equal_the_manifest(self): ...       # shrink-only, as `compare`
      def test_every_row_has_a_reason(self): ...
  ```
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams`. Every test must fail with `NameError` for `rule_violations`.
- [ ] **Step 3: Implement** `rule_violations` as an AST walk over `drover/` and `hub/`. Worded per section 13, including R4's module list. R3's `Answer.NO` part reads function bodies. `scripts/deploy_hub.py` and `scripts/test.py` are outside. Write the manifest from what the tree shows at 92ce946, each row with a reason. R1 has 51 importing modules [M], and 53 rows by an import walk, because `disk_cleanup` and `hub/auth` each import in two places; the manifest holds what the rule finds. R6's count is whatever the tree shows.
- [ ] **Step 4: Green.** `DROVER_TESTS=integration $PY -m unittest $GUARDS`, then `env -u DROVER_TESTS $PY -m tests.tiers tests.test_seams tests.test_guards`.
- [ ] **Step 5: Commit** `tests/test_seams.py tests/seam_exceptions.json`: "The seam guards pin every subprocess, replace, git argv, deletion and deferred import exception as it stands at the base."

### Task 4: Registration trace

**Files:**
- Create: `tests/registration_trace.py`, `tests/registration_trace.json`, `tests/test_registration_trace.py`

**Interfaces:**
- Produces: `tests.registration_trace.CASES: dict[str, Case]`; `record(side) -> dict`, hand-run only; `materialize(case_name, side, root, *, damaged=True) -> Path`, which rebuilds a case's admin tree as that side recorded it, damaged or as `before`, from recorded file bytes and uses no git; `recorded(case_name, side, root=None) -> dict(before=dict(stdout, rows, files), code, stdout, rows, files)`, with placeholders expanded against `root` when one is given: `before` is the good listing and admin bytes taken before the damage, for Task 9's compare.

The recorder runs real git by hand. It is not a `test_*` module, it is never imported at load by a test that spawns, and nothing marks it integration. So `INTEGRATION_CEILING` does not move.

- [ ] **Step 1: Failing test**, default tier:
  ```python
  class Replay(unittest.TestCase):
      def test_every_case_in_section_5_3_is_recorded_for_both_git_versions(self):
          self.assertEqual({c: set(TRACE[c]) for c in TRACE}, {c: {'windows', 'wsl'} for c in CASES})
      def test_entries_parses_each_recorded_listing(self):
          # through disk_worktrees.entries, with git faked at placement_git.subprocess
          for each case, side with code 0: self.assertEqual(entries(repo), recorded rows)
      def test_entries_raises_for_each_recorded_failure(self): ...
      def test_recording_holds_no_host_path(self):
          self.assertNotRegex(RAW, r'[A-Za-z]:[\\/]|/mnt/|/home/')
  ```
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_registration_trace` fails with `ModuleNotFoundError` for `tests.registration_trace`.
- [ ] **Step 3: Implement.** `CASES` are exactly the fixture of section 5.3: relative, absolute, drive-letter, `/mnt` and ext4 pointers; a pointer without `/.git`; locked with and without a reason, with trailing blank lines, and unreadable; `commondir` empty, a directory, wrong and missing; detached; garbage `HEAD`; main `HEAD` garbage or missing; main `refs` missing; broken `config`; an unknown repository extension; missing directory; empty `gitdir`; an admin directory with only `locked`; a stray file; a directory vanishing mid-read; bare repos; main checkout. `record` builds each with real git in a temp tree, stores the listing and admin-file bytes as `before`, damages it, runs the listing again, and stores exit code, stdout, parsed rows and admin-file bytes, with paths replaced by placeholders. `unreadable` and `vanishing` are stored as markers: `materialize` renders `unreadable` as a directory at that name, so the open fails with no patch, and `vanishing` as absent. Placeholders keep a pointer's form (`<DRIVE>:/...`, `<MNT>/...`), so the drive-letter and `/mnt` cases stay apart. `CASES` keys are the fixture's words, among them `broken config`, `unknown extension` and `main refs missing`, which Task 9's `UNSEEN` names.
- [ ] **Step 4: Record by hand.** From the worktree: `env -u DROVER_TESTS $PY -m tests.registration_trace --side windows`, then the same under WSL with `--side wsl`, using a WSL interpreter and a temp tree on ext4. Temp trees only. Then green: `DROVER_TESTS=integration $PY -m unittest tests.test_registration_trace $GUARDS`, and the purity line for `tests.test_registration_trace`.
- [ ] **Step 5: Commit** the three files: "A hand-recorded registration trace pins what Windows and WSL git do over the whole damage fixture, and the default tier replays it."

### Task 5: Decision trace harness and stage 4 scenarios

**Files:**
- Create: `tests/disk_trace.py`, `tests/disk_decision_trace.json`, `tests/test_disk_decision_trace.py`

**Interfaces:**
- Consumes: `tests.disk_fixture`; `tests.placement_trace.normalize` for placeholders.
- Produces: `disk_trace.collect() -> dict`; `Scene`, a builder of a temp tree holding real admin files; `TraceGit`, which stands in for `subprocess` at each module in `PATCH_TARGETS`, at the base `placement_git` and `disk_paths`, and answers by meaning, not order; `disk_trace.compare(old, new) -> list[Change]` with `Change.direction` in `('towards_keep', 'report_added', 'towards_deletion', 'neutral')`; CLI `python -m tests.disk_trace --write | --compare OLD NEW`; `disk_trace.PATCH_TARGETS`, the one tuple later slices repoint (Tasks 15, 16).

`TraceGit` derives `worktree list` from the admin files, so slice 1's proof reads a tree that agrees with the listing. It answers `status`, `rev-parse`, `ls-files`, `repos.load`'s reads, `read-tree`, `diff`, `update-ref`, and it applies `worktree remove`, `lock` and `unlock` to the tree. `cleanup_processes` is faked, because `disk_side.serve` calls it at :15. `uuid4`, `now` and the clock are fixed.

- [ ] **Step 1: Failing tests**, default tier:
  ```python
  class Trace(unittest.TestCase):
      def test_trace_matches_the_recording(self):
          self.assertEqual(collect(), json.loads(TRACE.read_text()))
      def test_trace_is_host_neutral(self):
          self.assertNotRegex(TRACE.read_text(), r'[A-Za-z]:[\\/]|/mnt/|drover-')
      def test_each_scenario_freezes_rows_errors_journal_and_deletions(self):
          keys = {'rows', 'errors', 'journal', 'deleted'}          # rows: (path, action, reason_code), head, branch, side_lock
          for scene in TRACE_DATA.values(): self.assertEqual(set(scene), keys)
      def test_head_is_not_frozen_on_kept_rows(self): ...
      def test_every_decide_reason_has_a_scenario(self):
          # reason codes read from decide's AST by the test, so a new code fails here
          self.assertLessEqual(reasons_in_decide(), reasons_in(TRACE_DATA))
  class Compare(unittest.TestCase):
      def test_keep_to_removed_is_towards_deletion(self): ...
      def test_candidate_to_keep_is_towards_keep(self): ...
      def test_new_deleted_path_is_towards_deletion_whatever_the_row_says(self): ...
      def test_added_report_row_is_report_added(self): ...
      def test_changed_keep_reason_is_towards_keep(self): ...
      def test_lost_error_row_is_towards_deletion_and_a_new_one_is_report_added(self): ...
  ```
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_disk_decision_trace` fails with `ModuleNotFoundError` for `tests.disk_trace`.
- [ ] **Step 3: Implement** the harness, running today's `disk_side.serve` for stage 4, dry and applied. Scenarios: every `decide` reason (`other_side_or_lock_note`, `other_drive`, `main_checkout`, `busy` by process and by live task, `protected`, `missing`, `dirty`, `orphan`, `settled`, `live_owner`, `ignored_content`, `quarantine_elapsed`, `nested_worktree`, `identity_changed`, and `revalidation_changed` twice: `current` no longer a candidate at disk_worktrees.py:342-344, a worktree gone dirty mid-stage, which the proof cannot see and which keeps the reason through Task 10, and a mid-stage registration change, the one Task 10 may move); each quarantine step (started, reset, elapsed, identity changed); lost registration; approved removal; a target under a main checkout; `unlisted` folder; `settled_deadline` in both places; a repo that cannot be listed.
- [ ] **Step 4: Record and green.** `env -u DROVER_TESTS $PY -m tests.disk_trace --write`, read the JSON row by row against today's code, then `DROVER_TESTS=integration $PY -m unittest tests.test_disk_decision_trace $GUARDS`, and the purity line. Time must be under 10 s.
- [ ] **Step 5: Commit** the three files: "The decision trace freezes stage 4's rows, journal and deletions through disk_side.serve with git faked at the process edge."

### Task 6: Decision trace, stages 2, 3, 5 and 6

**Files:**
- Modify: `tests/disk_trace.py`, `tests/disk_decision_trace.json`, `tests/test_disk_decision_trace.py`

**Interfaces:**
- Consumes: `Scene`, `TraceGit` from Task 5.

- [ ] **Step 1: Failing tests.**
  ```python
  def test_every_output_evidence_rejection_has_a_scenario(self):
      self.assertLessEqual({'main_checkout', 'not_directory', 'unsafe_path', 'owner_side', 'physical_drive',
                            'process using worktree', 'windows process using worktree', 'idle'}, rejections_in(TRACE_DATA))
  def test_idle_waiver_is_pinned_both_ways(self): ...   # hold and critical waive it; a budget never does
  def test_every_stage_has_a_scenario_and_every_deadline_row_appears(self):
      self.assertLessEqual({'settled_deadline', 'output_deadline', 'budget_deadline'}, reasons_in(TRACE_DATA))
  ```
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_disk_decision_trace`: all three fail, naming the missing set.
- [ ] **Step 3: Add scenarios.** Stage 2 temp: stale, protected subtree, `temp_root_elapsed`, `temp_root_partial`. Stage 3 caches: declared argv, folder cache by age and by level, `protected`, process busy. Stage 5 disposable: `declared_disposable` removed; live, nested, tracked, linked and repository-holding paths; `drive_unproven`; `output_evidence_unavailable`; `repository_unavailable`. Budget purge: met, shortfall, deadline, used unknown, processes unknown. Stage 6 report: each row kind, `budget_usage` incomplete, `merged_live_task`, `declared_report`, and a repo error.
- [ ] **Step 4: Record and green.** Same commands as Task 5.
- [ ] **Step 5: Commit** the three files: "The decision trace now covers the temp, cache, disposable, budget and report stages, including every rejection and deadline row."

### Task 7: Checkpoint after slice 0

**Files:**
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-checkpoint-0.md`
- Modify: `docs/superpowers/reviews/2026-11-02-rearchitecture-test-times.md`

Runs the Checkpoint protocol with `LAST_CP` as 92ce946.

- [ ] **Step 1: Run the protocol.** Spawn counts in the checkpoint report must equal Task 1's: nothing changed in production. Confirm with `git diff --stat 92ce946 -- drover hub`, which must print nothing.
- [ ] **Step 2: Reviewer focus.** Completeness: every `reason_code`, `output_evidence` rejection, deadline row and fixture case of sections 5.3 and 10 is pinned, and the trace would change if a rule did. Scope: no production change, and R1 to R6 only. Efficiency: `test_seams` and the trace test against target, and duplicated scenarios. There is no production hot path yet.
- [ ] **Step 3: Fix findings**, by the protocol, then record `Task 7: complete`.
- [ ] **Step 4: Commit** both reports: "The slice 0 checkpoint records its test times and its three reviews."

## Slice 1: real listing, fresh proof

### Task 8: `git_worktrees.listing` and `Listings`

**Files:**
- Create: `drover/git_worktrees.py`, `tests/test_git_worktrees.py`
- Modify: `drover/disk_worktrees.py`, `drover/disk_paths.py`, `drover/disk_registrations.py`, `drover/disk_side.py`, `tests/test_registration_trace.py`, `tests/seam_exceptions.json`, `tests/test_seams.py`

**Interfaces:**
- Consumes: `placement.git` until slice 4; `tests.registration_trace`.
- Produces, as section 5.3 with 3M3: `LISTING_TIMEOUT_S = 400.0`; `RegistrationsUnknown(OSError)`; `Listing(repo, rows)` with `.row(path)`; `Listings(repos, *, timeout_s: Callable[[], float] = lambda: LISTING_TIMEOUT_S, side=None)` with `of`, `every`, `removed`, `repaired`, and `common_dir(repo) -> str`, once per repo per stage; `listing(repo, *, timeout_s: float, side=None) -> Listing`.
- `request['_listings']` until Task 33.

`disk_worktrees.entries` stays as a thin call to `listing` until Task 10, so removal stays as strong as it is until the proof lands.

- [ ] **Step 1: Failing tests**, `tests/test_git_worktrees.py`, default tier, faking at `placement_git.subprocess`:
  ```python
  def test_listing_timeout_is_400_seconds(self): ...          # the fake's expected options carry timeout=400.0
  def test_failure_raises_registrations_unknown_with_todays_message(self):
      with self.assertRaises(RegistrationsUnknown) as caught: listing(repo, timeout_s=400.0)
      self.assertIsInstance(caught.exception, OSError); self.assertIn('fixture stderr', str(caught.exception))
  def test_timeout_is_a_failure_of_the_whole_repo(self): ...
  def test_each_repo_is_listed_once_on_first_use(self):       # 3 of(), 1 call
  def test_constructor_lists_nothing(self): ...
  def test_failure_is_kept_and_reraised_without_listing_again(self): ...   # of, of, every: one call, same error
  def test_every_lists_the_rest_then_raises_if_any_failed(self): ...
  def test_timeout_callable_is_called_at_each_listing(self):  # values 400.0 then 7.0 reach two repos' commands
  def test_removed_drops_the_row_and_repaired_expects_the_pointer(self): ...
  def test_main_checkout_row_comes_first(self): ...
  def test_two_spellings_of_one_repo_list_once_and_share_removed(self):   # Review Focus 5
      listings = Listings(['/repo', '/repo/']); listings.of('/repo'); listings.of('/repo/')
      self.assertEqual(len(fake.calls), 1)
      listings.removed('/repo/', path); self.assertIsNone(listings.of('/repo').row(path))
  ```
  Repoint `test_entries_*` in `tests/test_registration_trace.py` to `listing`.
  Add to `tests/test_seams.py`: `test_r8_literal_only_in_git_worktrees_and_its_exceptions`, and the R5 row test for `git_worktrees`.
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_git_worktrees` fails with `ModuleNotFoundError` for `drover.git_worktrees`.
- [ ] **Step 3: Implement.** Move `entries`' body into `listing` with the same keys and `cleanup_path` spelling. `Listings` keys by `placement_paths.cleanup_parts`. `disk_side.serve` sets `request['_listings'] = Listings([m['repo'] for m in request['repos']])`, which costs nothing until first use. Stage 4 line 247, stage 5 lines 356 and 361, and stage 6 line 446 read it. Today's 60 s goes for listing only: other evidence keeps it (section 10, slice 1). Add R5's temporary row, `git_worktrees` to `placement`, and R8's five exceptions: cleanup:49, origin:27, pipeline:106, placement:153, hub/task_views:120.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_git_worktrees tests.test_registration_trace tests.test_disk_decision_trace $(mods 'entries|_listings')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
  The decision trace must not change here: stage order and error rows stay. A fake of `entries` in a changed test moves to `listing`, and the old patch goes.
- [ ] **Step 5: Commit**: "Each stage run now lists each repo once on a 400 s timeout and keeps a failure for every later use."

### Task 9: `parse` and `prove`

**Files:**
- Modify: `drover/git_worktrees.py`, `tests/test_git_worktrees.py`, `tests/test_registration_trace.py`, `tests/test_seams.py`

**Interfaces:**
- Consumes: `Listings`, `Listings.common_dir`; `registration_trace.materialize`, `recorded`; Task 2's survey.
- Produces, as section 5.3: `Refusal(repo, worktree, why, detail)`; `Proof(refused, reach)`; `AdminFiles`; `parse(admin, files)`; `prove(listings, repo, worktree, *, pointer: bool, timeout_s: float, side=None) -> Proof`. `prove` applies the table's middle column itself, so `Proof.refused` holds only what refuses this removal: an unknown pointer (`worktree=None`) in another repo is not in it. `why` then picks the row: `'changed'` as today, `'unknown'` is `registration_unknown`.

- [ ] **Step 1: Failing tests**, temp trees, no fake of the reader:
  ```python
  class ProofTable(unittest.TestCase):   # one test per row of section 5.3's table, asserting scope (which targets refuse) and why
      def test_target_row_differs_refuses_this_removal(self):
          for field in ('pointer', 'locked', 'HEAD', 'resolved HEAD', '.git present'): subTest ...
      def test_target_file_unreadable_refuses_this_removal_as_unknown(self): ...
      def test_any_row_locked_or_commondir_bad_refuses_every_removal(self): ...
      def test_main_head_unlike_main_row_refuses_every_removal(self): ...
      def test_reftable_directory_refuses_every_removal(self): ...
      def test_read_past_timeout_refuses_every_removal(self): ...      # timeout_s=0, a detached target, so no tip spawns
      def test_listed_row_with_no_admin_directory_refuses_every_removal(self): ...
      def test_pointer_the_listing_lacks_refuses_every_removal(self): ...
      def test_own_stage_removed_and_repaired_are_not_differences(self): ...
      def test_empty_pointer_refuses_every_removal_in_its_own_repo_only(self): ...   # ruling 9
      def test_clean_tree_refuses_nothing_and_reach_covers_rows_and_pointers(self): ...
  class Reading(unittest.TestCase):
      def test_gitdir_strips_line_ending_and_one_trailing_dot_git(self): ...
      def test_relative_pointer_resolves_from_admin_directory(self): ...
      def test_drive_letter_pointer_puts_its_true_place_in_reach(self): ...      # O3, as Task 2 found
      def test_only_not_found_with_admin_present_means_unlocked(self): ...
      def test_unreadable_locked_never_reads_as_unlocked(self): ...
      def test_garbage_head_differs(self): ...
      def test_stray_file_is_ignored(self): ...
      def test_non_target_rows_compare_pointer_only_not_branch_or_lock_text(self): ...   # ruling 14
      def test_repo_with_no_worktrees_directory_agrees(self): ...                         # Review Focus 1
      def test_non_ascii_path_and_crlf_pointer_compare_equal(self): ...                   # Review Focus 2
      def test_undecodable_bytes_refuse_without_raising(self): ...                        # Review Focus 2
  class Cost(unittest.TestCase):
      def test_one_prove_reads_every_repo_and_spawns_one_tip_for_a_branch_target(self): ...
      def test_detached_target_spawns_no_tip(self): ...
      def test_common_dir_is_one_spawn_per_repo_per_stage(self): ...
      def test_failed_listing_anywhere_raises_from_prove(self): ...
      def test_tip_failure_raises_registrations_unknown_and_keeps_the_row(self): ...   # Task 23 restates it to GitFailed
  ```
  In `tests/test_registration_trace.py`, the design's real-git test, as replay (ruling 16). The real git ran by hand in Task 4, nothing real runs here, and `INTEGRATION_CEILING` does not move:
  ```python
  def test_prove_refuses_wherever_recorded_git_exited_non_zero(self):
      # for each case and side with code != 0: materialize(case, side, root), prove against recorded(case, side, root)['before'] -> refused
      # except the cases no admin file shows, named exactly:
      UNSEEN = {'broken config', 'unknown extension', 'main refs missing'}
  def test_prove_agrees_wherever_the_recorded_row_is_whole(self): ...
  ```
  Add `test_r9_only_named_callers_reach_prove_and_nothing_names_parse` to `tests/test_seams.py`.
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_git_worktrees tests.test_registration_trace` fails with `ImportError`/`AttributeError` for `prove` and `parse`.
- [ ] **Step 3: Implement** by section 5.3's reading rules: `_read_admin(common_dir: str, *, timeout_s: float) -> dict[str, AdminFiles]` reads in-process on 8 threads, a read past `timeout_s` refuses, `parse` pure. `prove` calls `listings.every()` first. Read bytes, decode strictly, and refuse on failure. Never add a row. A failed tip goes through `placement.git`, so it raises `RegistrationsUnknown` until Task 23.
- [ ] **Step 4: Green.**
  ```bash
  DROVER_TESTS=integration $PY -m unittest tests.test_git_worktrees tests.test_registration_trace $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
  If `UNSEEN` would need another case, report BLOCKED: that is a gap in ruling 14's accepted list.
- [ ] **Step 5: Commit**: "The fresh proof reads every repo's admin files, compares them with the stage listing and can only refuse."

### Task 10: Removal takes one proof; `entries` goes

**Files:**
- Modify: `drover/disk_worktrees.py`, `drover/disk_paths.py`, `drover/disk_registrations.py`, `tests/disk_trace.py`, `tests/disk_decision_trace.json`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`, tests that fake `entries` or use `registered=`
- Create: `tests/test_disk_fresh_proof.py`, `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-1.md`

**Interfaces:**
- Consumes: `prove`, `Listings`, `Proof.reach`.
- Produces: `overlaps_worktrees(request, target, own, *, reach)`, no listing and no `registered=`, until Task 39.

- [ ] **Step 1: Failing tests**, `tests/test_disk_fresh_proof.py`, default tier, through `Scene` and `TraceGit`:
  ```python
  def test_a_failed_listing_in_one_repo_keeps_every_removal_in_every_repo_and_writes_no_stamp(self): ...
  def test_a_commit_after_the_listing_keeps_the_unit_whatever_the_first_pass_read(self): ...
  def test_a_stages_own_worktree_remove_does_not_refuse_its_next_removal(self): ...
  def test_a_target_inside_another_repos_main_checkout_is_refused(self): ...
  def test_an_unreadable_locked_never_reads_as_unlocked(self): ...
  def test_each_refusal_scope_gives_its_row(self):      # identity_changed, output_evidence_unavailable, registration_unknown
  def test_one_removal_lists_nothing_and_proves_once(self):
      self.assertEqual(fake.count('worktree', 'list'), 0)        # at removal
      self.assertEqual(proof_calls, 1)
  def test_reregister_keeps_its_one_real_listing_and_tells_listings(self): ...
  def test_proof_rows_exist_only_in_applied_runs(self): ...
  ```
  Add `drover.disk_worktrees.entries` and `drover.disk_registrations._entries` to `qualified_names`.
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_disk_fresh_proof tests.test_architecture`. The architecture test must name `entries`, and `test_one_removal_lists_nothing_and_proves_once` must show a removal that reached `worktree list`.
- [ ] **Step 3: Implement**, per section 10's slice 1:
  - At disk_worktrees :312 and disk_paths :290, a row from the stage listing with no git. No row on a registered candidate keeps.
  - At :333 and :302, one `prove`; `pointer` is False for a `missing` candidate.
  - `overlaps_worktrees` measures against `Proof.reach` at removal and listing rows at first pass.
  - `discard_at` calls `Listings.removed`; `reregister` calls `Listings.repaired`, and keeps its real listing at :272.
  - Delete `entries`, `_entries`, `registered=`.
  - Repoint `tests/disk_memory.py` and every test faking `entries` to `listing`; the old patches go.
  - `disk_worktrees` has 3 spare lines and `disk_paths` 7: `entries` leaving pays.
- [ ] **Step 4: Re-record and compare.**
  ```bash
  git show $SLICE_BASE:tests/disk_decision_trace.json > "$TEMP/old-trace.json"
  env -u DROVER_TESTS $PY -m tests.disk_trace --write
  $PY -m tests.disk_trace --compare "$TEMP/old-trace.json" tests/disk_decision_trace.json
  ```
  Allowed, each towards keeping: a removal the proof refuses; a mid-stage change now `identity_changed` rather than `revalidation_changed`; a target nesting in a drive-letter pointer. Any `towards_deletion` row, or any other change: report BLOCKED, do not commit the record.
- [ ] **Step 5: Green.**
  ```bash
  M="tests.test_disk_fresh_proof tests.test_disk_decision_trace tests.test_git_worktrees $(mods 'entries|registered=|overlaps_worktrees|disk_memory')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 6: Timing, read-only.** From WSL, time `git_worktrees._read_admin` alone over drover, PanGloss and motif on `/mnt/h`. Do not time `prove`: its `every()` runs real listings of up to 265 s. No cleanup. Compare with section 2.5's 7.2 s, and write it in the slice report with the changed trace rows: owner check 8 reads this figure.
- [ ] **Step 7: Slice exit.** `listing` is once per repo per stage; both removal paths take one proof; R8's five, R9 live, R5's one temporary row; removed symbols added; trace check clean. Commit: "Removal now takes its row from the stage listing and one fresh proof, so a repo's listing cost is paid once per stage."

## Slice 2: `durable`

### Task 11: `durable`

**Files:**
- Create: `drover/durable.py`, `tests/test_durable.py`
- Modify: `tests/test_runner_status_write.py`, `tests/test_seams.py`, `tests/seam_exceptions.json`

**Interfaces:**
- Produces, as section 5.4: `Wait`; `NO_WAIT, ROUTINE, ORDINARY, RELEASE, TERMINAL`; `write_bytes`, `write_text`, `write_json`, `publish`, `move`, `remove`, `rmdir`. `sync` is required. Stdlib only.

- [ ] **Step 1: Failing tests.** `tests/test_durable.py`, patching `durable.os.replace` and `durable.time`, no real sleep. The write tests of `tests/test_runner_status_write.py` **move** here and leave that file; its supervision tests stay.
  ```python
  def test_wait_constants(self): self.assertEqual([w.seconds for w in (NO_WAIT, ROUTINE, ORDINARY, RELEASE, TERMINAL)], [0, .5, 2, 5, 30])
  def test_sync_has_no_default(self): ...                       # TypeError
  def test_refused_replace_retries_until_the_wait_runs_out(self): ...
  def test_only_permission_error_is_retried(self): ...
  def test_a_write_that_lands_reads_no_clock(self): ...         # hot path: monotonic mock not called
  def test_last_permission_error_is_raised_and_temp_is_gone(self): ...
  def test_target_whole_or_unchanged_on_failure(self): ...
  def test_temp_is_unique_and_in_the_target_folder(self): ...   # mkstemp args, prefix name., suffix .tmp
  def test_posix_temp_takes_umask_default_not_0600(self): ...
  def test_write_json_keeps_each_encoding(self): ...            # indent 1, allow_nan; indent None, allow_nan False raises
  def test_publish_removes_temp_on_failure_and_syncs_directory_when_asked(self): ...
  def test_move_does_no_temp_handling_and_no_wait_is_one_attempt(self): ...
  def test_remove_and_rmdir_outlast_a_reader_and_missing_ok(self): ...
  def test_disk_full_leaves_target_whole_removes_temp_and_raises_at_once(self): ...   # Review Focus 3: ENOSPC at write and at fsync
  def test_missing_parent_raises_at_once_and_makes_no_directory(self): ...
  def test_two_threads_publishing_one_path_both_land_whole(self): ...
  ```
  In `tests/test_seams.py`: `test_durable_imports_no_drover_module`, the live R5 rule.
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_durable` fails with `ModuleNotFoundError` for `drover.durable`.
- [ ] **Step 3: Implement** per section 5.4's invariants and errors. The clock starts at the first refusal.
- [ ] **Step 4: Green.**
  ```bash
  DROVER_TESTS=integration $PY -m unittest tests.test_durable tests.test_runner_status_write $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "One durable module now publishes, moves and removes files with a stated wait for a brief Windows reader."

### Task 12: Runner files, compaction hold, disk state

**Files:**
- Modify: `drover/runner_common.py` and the 20 `write_json_atomic` call sites, `drover/compaction_hold.py`, `drover/disk_state.py`, `tests/fakeci.py`, `tests/test_durable.py`, `tests/test_disk_state.py`, `tests/test_compaction_hold.py`, `tests/__init__.py`, `tests/test_guards.py`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`

**Interfaces:**
- Consumes: `durable`.
- `runner.write_json_atomic` is re-exported today, and `tests/fakeci.py:198` uses it: repoint to `durable.write_json(..., sync=False)`.

- [ ] **Step 1: Red.** Add `drover.runner_common.write_json_atomic`, `drover.compaction_hold._retried` and `drover.disk_state._outlast_readers` to `qualified_names`. Add tests:
  ```python
  # tests/test_durable.py
  def test_status_routine_heartbeat_waits_half_a_second_and_terminal_thirty(self): ...   # Status through patched replace
  def test_status_routine_keeps_nan_writable(self): ...                                  # allow_nan=True at the site
  # tests/test_disk_state.py
  def test_lock_release_outlasts_a_reader_for_five_seconds(self): ...   # replaces the real-sleep retry test
  def test_state_write_syncs_and_waits_ordinary(self): ...
  # tests/test_compaction_hold.py
  def test_hold_write_goes_through_save_file_and_the_ordinary_wait(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_durable tests.test_disk_state tests.test_compaction_hold`. Must name the three symbols, and the wait tests must fail on the old waits.
- [ ] **Step 3: Implement** by section 5.4's wait table: `Status.routine` ROUTINE; other run files ORDINARY; terminal and `record.json` TERMINAL; `disk_state._write` and `save_file` ORDINARY with `sync=True`, `indent=None`, `allow_nan=False`; the mkdir stays in `save_file`, which stays as the naming function. `compaction_hold._write` keeps calling `save_file`. Delete `REPLACE_WAIT_S`, `TERMINAL_WAIT_S`, `REPLACE_STEP_S`, `RETRIES`, `RETRY_S`, `RELEASE_WAIT_S`. Take the sleep out of `test_disk_state`; if nothing else there is impure, remove it from `tests.IMPURE` and lower the ceiling here.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_durable tests.test_disk_state tests.test_compaction_hold tests.test_runner_status_write tests.test_artifact_records tests.test_runner $(mods 'write_json_atomic|_retried|_outlast_readers|save_file')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Run files, the compaction hold and disk state now write through durable, and their three retry idioms are gone."

### Task 13: Remaining writers and moves

**Files:**
- Modify: `drover/allowance.py`, `drover/handoff.py`, `drover/launch_handle.py`, `drover/wizard_files.py`, `drover/config.py`, `drover/disk_schedule.py`, `drover/placement_git.py`, `drover/disk_registrations.py`, `drover/disk_cleanup.py`, `tests/test_durable.py`, `tests/seam_exceptions.json`
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-2.md`

**Interfaces:**
- Consumes: `durable`.
- `hub/reload_protocol.atomic_json` stays local (ruling 15): `test_standalone_import` pins a stdlib-only module. No Windows reader reaches its target, so it takes no copy of the retry: its one production caller, hub/runtime.py:483, writes into a private `mkdtemp` staging directory before the rename.

- [ ] **Step 1: Red.** Delete the R2 rows of these modules, leaving disk_state:119's `<function>`, hub/runtime:496's, task_access:83's and `hub/reload_protocol`'s `atomic_json`, whose reason now states the ruling and the staging directory. Add:
  ```python
  # tests/test_durable.py
  def test_launch_handle_tail_swallows_oserror_and_waits_routine(self): ...
  def test_registration_move_is_one_attempt(self): ...      # NO_WAIT at disk_registrations:266
  ```
  `test_artifact_records`' temp pattern must still match; change nothing there.
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams tests.test_durable`. `test_seams` must name each module still calling `os.replace`.
- [ ] **Step 3: Implement** by the wait table. `_native_metadata_io` ORDINARY with sync. `placement_git._METADATA_IO` stays, with a docstring saying the guard cannot see it. Moves: handoff:110, disk_registrations:266 and disk_cleanup:81 use `durable.move`. `compaction_hold.claim`, `owner.json` and `reload_protocol`'s create-once requests stay. `hub/reload_protocol.py` is not touched, so its `OVER` pin of 571 stands.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_durable tests.test_allowance tests.test_handoff tests.test_launch_runner_handle tests.test_wizard tests.test_config_layout tests.test_disk_schedule tests.test_disk_registrations tests.test_disk_cleanup tests.test_disk_decision_trace"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Slice exit.** Every replace but R2's goes through `durable`; R2 holds three stated renames and `atomic_json`; the three symbols removed; trace check prints nothing. Write the slice report. Commit: "Every temp-then-replace writer and file move outside the frozen bootstrap now goes through durable, leaving four stated exceptions."

## Slice 3a: `process`, placement and disk callers

### Task 14: `process`, and fixing the call lists

**Files:**
- Create: `drover/process.py`, `tests/test_process.py`
- Modify: `tests/fakeci.py`, `tests/test_seams.py`
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-3a.md`

**Interfaces:**
- Produces, as section 5.1: `Kind`; `Command(argv, timeout_s, cwd=None, env=None, stdin=None, sink=None)`; `Result(kind, code, out, err, elapsed_s, detail)` with `.ok`, `.stdout`, `.stderr`; `CommandTooLong(ValueError)`; `run(command) -> Result`; `NO_WINDOW`; `list2cmdline`, a plan addition to section 5.1, for placement_host:321, disk_schedule_steps:64 and `wsl_argv`. `wsl_argv` arrives in Task 20.
- Produces in `tests/fakeci.py`: `process_options(timeout, *, cwd=None, env=None, input=None, sink=None) -> dict`, the one statement of `process.run`'s fixed options.

- [ ] **Step 1: Fix the lists.** Read-only, before anything moves. Write in the slice report: every call with no timeout today (section 5.1's table is what the review found, not proof of a complete list), and every `text=True` site without an `encoding`, marking which keep locale decoding through `Result.out`.
- [ ] **Step 2: Failing tests**, `tests/test_process.py`, default tier, `FakeCommands.patch(process)`, with `process.time` patched where elapsed matters:
  ```python
  def test_exit_is_kept_with_bytes(self): r = run(cmd); self.assertEqual((r.kind, r.code, r.out), ('exited', 3, b'x'))
  def test_ok_only_for_exit_zero(self): ...
  def test_stdout_decodes_utf8_with_replace(self): ...
  def test_out_keeps_bytes_a_locale_site_decodes_itself(self): self.assertEqual(r.out, 'é'.encode('cp1252'))
  def test_timeout_is_timed_out_and_keeps_partial_output(self): ...
  def test_elapsed_at_the_timeout_is_timed_out_with_output_kept(self): ...      # the startup race, placement_git.py:121
  def test_missing_executable_and_refused_spawn_are_unavailable(self): ...
  def test_timeout_must_be_positive(self): ...                    # 0, -1, None: ValueError
  def test_stdin_is_devnull_unless_a_payload_is_given(self): ...
  def test_str_and_bytes_payloads_reach_the_child(self): ...
  def test_sink_takes_stdout(self): ...
  def test_every_command_carries_no_shell_and_the_console_flag(self):
      with mock.patch.object(process.subprocess, 'run', return_value=done) as spawn: run(cmd)
      seen = spawn.call_args.kwargs                                      # what process.run really passed
      self.assertIs(seen['shell'], False)
      self.assertEqual(seen.get('creationflags'), process.NO_WINDOW.get('creationflags'))
  def test_base_exception_propagates(self): ...
  def test_a_process_failure_raises_nothing(self): ...
  def test_list2cmdline_quotes_as_subprocess_does_under_the_fake(self): ...
  ```
  In `tests/test_seams.py`: the live R5 rule for `process`, imports only `placement_paths` and `compaction_hold`. `compaction_hold` is reached from Task 20.
- [ ] **Step 3: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_process` fails with `ModuleNotFoundError` for `drover.process`.
- [ ] **Step 4: Implement.** Ordering: spawn, wait, elapsed check. `run` calls `process.subprocess.run` once, with one fixed option set; its real adapter `_run_tree` spawns with `Popen` so a timeout ends the whole tree (spec Appendix C, tree kill), and tests replace that one reference. A required timeout with no default on `Command`. `list2cmdline` is bound at import, `from subprocess import list2cmdline`, because `FakeCommands` refuses any unscripted call through `process.subprocess`.
- [ ] **Step 5: Green.**
  ```bash
  DROVER_TESTS=integration $PY -m unittest tests.test_process $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 6: Commit**, with the slice report: "One process module now runs each short-lived command with a required timeout, no shell, the console flag and one outcome."

### Task 15: Placement callers

**Files:**
- Modify: `drover/placement_exec.py`, `drover/placement_git.py`, `drover/placement.py`, `drover/placement_host.py`, `drover/placement_process.py`, `drover/placement_proof.py`, `drover/placement_worker.py`, `tests/disk_trace.py`, `tests/test_guards.py`, `tests/test_process.py`, `tests/test_git_worktrees.py`, `tests/test_registration_trace.py`, `tests/placement_trace.json`, `tests/seam_exceptions.json`
- Modify: tests that patch these modules' `subprocess`

**Interfaces:**
- Consumes: `process.run`, `Command`, `process_options`.
- `placement.git` and `placement_git.git` keep their conventions until Task 28, with `placement_git.py:119`'s `runner or subprocess.run` gone. Those conventions still need `subprocess.TimeoutExpired` and `CompletedProcess`, so `placement_git` keeps its import and its R1 row until Task 28.
- `_cim_rows` raises the builtin `TimeoutError`, an `OSError`, on `kind == 'timed_out'`. placement_process:415 and placement_proof:118 test for it in place of `subprocess.TimeoutExpired`, so `process_query_timeout` stays.
- `disk_trace.PATCH_TARGETS` swaps `placement_git` for `process`, because git now reaches the fake there. The decision trace itself must not change.

- [ ] **Step 1: Red.** Delete R1 rows for `placement`, `placement_host`, `placement_process`, `placement_worker`. Keep `placement_exec`'s: ADR-0002. Restate the fakes per Conventions, including those Tasks 8 and 9 put at `placement_git.subprocess` in `tests/test_git_worktrees.py` and `tests/test_registration_trace.py`, which no longer reach git. In `tests/test_guards.py`, `test_patch_is_local_...` is **rewritten** against `process`, asserting `process.subprocess` is restored, and `test_patched_git_preserves_...` and the gate test's `options` at :259 are restated at `process`. `test_git_helper_uses_...` tests `gitfix.sh`, not production code, and stays as it is (section 5.2). Add:
  ```python
  # tests/test_process.py
  def test_placement_exec_155_placement_git_221_placement_168_go_through_process(self): ...    # one fake, three call sites
  def test_reg_and_icacls_output_keep_non_ascii_paths(self): ...      # placement_host:72, placement_process sites: decoded from Result.out by locale
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams tests.test_guards tests.test_process`. `test_seams` must name the four modules, and the guard tests must show `unused command expectations`.
- [ ] **Step 3: Migrate** the sites of section 5.1's Placement row: placement_exec:155, placement_git:221, placement:168, placement_host:72, :131, placement_process:100, :134, :310, placement_worker:167. Where `Result` replaces a catch tuple, the tuple goes, and it loses `subprocess` where that was its only use. `placement_host`'s three `run=` defaults go, and its tests take `process`-level fakes. placement_host:321 calls `process.list2cmdline`. The `NO_WINDOW` imports at placement_git:22, placement_host:18 and placement_process:16 go with their last use. Sites not in the Task 14 list stay out.
- [ ] **Step 4: Re-record** `tests/placement_trace.json` from `placement_trace.collect()`, in its existing format. Only `kwargs` may change: `text` goes. Read the diff, and put the summary in the slice report. Any change in a command or an answer: report BLOCKED.
- [ ] **Step 5: Green.**
  ```bash
  M="tests.test_process tests.test_placement tests.test_disk_decision_trace tests.test_git_worktrees tests.test_registration_trace tests.test_disk_fix6 tests.test_disk_fix7 $(mods "placement(_exec|_git|_host|_process|_worker)?\.subprocess|patch\(placement_git|'subprocess\.run'|placement_host")"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 6: Commit**: "Placement's short-lived commands now run through process, with their fakes restated at that one seam."

### Task 16: Disk callers

**Files:**
- Modify: `drover/disk_runtime.py`, `drover/disk_side.py`, `drover/disk_paths.py`, `tests/disk_trace.py`, `tests/placement_trace.json`, `tests/seam_exceptions.json`, `tests/test_disk_trim.py`, `tests/test_disk_worker_status.py`, `tests/__init__.py`, `tests/test_guards.py`

**Interfaces:**
- Consumes: `process.run`.
- `disk_trace.PATCH_TARGETS` drops `disk_paths`, whose cache command now reaches the fake through `process`. The decision trace itself must not change.

- [ ] **Step 1: Red.** Delete R1 rows for `disk_runtime`, `disk_side`. `disk_paths` keeps its row until 6c if only exception types remain; state which. Add:
  ```python
  # tests/test_disk_worker_status.py
  def test_cleanup_worker_starts_with_the_console_flag(self): ...       # both disk_runtime:92 and :141, through process
  def test_worker_timeout_keeps_todays_error_row(self): ...
  # tests/test_disk_trim.py, test restated
  def test_trim_failure_and_timeout_keep_trim_unavailable(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams tests.test_disk_worker_status tests.test_disk_trim`. `test_seams` must name `disk_runtime` and `disk_side`, and the flag test must fail with `unused command expectations` or a real-process refusal, because the command does not yet reach `process`: 92ce946 already passes the flag at disk_runtime.py:94 and :142.
- [ ] **Step 3: Migrate** disk_runtime:92, :141, disk_side:44, disk_paths:161. `disk_runtime`'s rc and output semantics are unchanged: `Result.kind == 'timed_out'` takes today's `TimeoutExpired` branch, `.stdout` the partial output. If `test_disk_trim` has no real spawn left, remove it from `tests.IMPURE` and lower `IMPURE_CEILING` in this commit.
- [ ] **Step 4: Re-record** `tests/placement_trace.json`, as Task 15. `git diff --exit-code $SLICE_BASE -- tests/disk_decision_trace.json` must print nothing.
- [ ] **Step 5: Green.**
  ```bash
  M="tests.test_process tests.test_placement tests.test_disk_decision_trace tests.test_disk_trim tests.test_disk_worker_status $(mods 'disk_(runtime|side)\.subprocess|cleanup_call|disk_caches')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 6: Slice exit.** R1 lost each module that dropped `subprocess`, trace check clean, slice report complete. Commit: "Cleanup's worker start and its other short-lived commands now run through process."

## Slice 3b: `process`, remaining callers

### Task 17: Other drover raw sites

**Files:**
- Modify: `drover/task_context.py`, `drover/wizard_detect.py`, `drover/resources.py`, `drover/decide.py`, `drover/stats.py`, `drover/decisions.py`, `drover/pane_files.py`, `tests/seam_exceptions.json`, tests patching these
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-3b.md`

**Interfaces:**
- Consumes: `process.run`.
- `decisions._git` keeps its raw shape over `process` until Task 26. `pane_files.py:80` stays an R3 row. `resources` and `pane_files` keep their R1 rows until Task 18, because their `run=subprocess.run` defaults stay.

- [ ] **Step 1: Red.** Delete R1 rows for `task_context`, `wizard_detect`, `decide`, `stats`, `decisions`, restate fakes per Conventions, and add `test_resources_145_keeps_locale_decoding` to `tests/test_resources.py`, from Task 14's list.
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams tests.test_resources tests.test_stats tests.test_task_context tests.test_wizard_detection tests.test_decisions tests.test_pane_launch_files`. Must name the five modules.
- [ ] **Step 3: Migrate** the "Other drover" row of section 5.1. `resources` is at 492: it must shrink. The stale exception types go with each site.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_resources tests.test_stats tests.test_task_context tests.test_wizard_detection tests.test_decisions tests.test_decide tests.test_pane_launch_files $(mods '(task_context|wizard_detect|resources|decide|stats|decisions|pane_files)\.subprocess')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 5: Commit**: "The remaining drover commands now run through process, and locale-decoded output keeps its non-ASCII paths."

### Task 18: Injected `run=` defaults

**Files:**
- Modify: `drover/compaction.py`, `drover/compaction_vhd.py`, `drover/herdr_client.py`, `drover/launch.py`, `drover/pane_files.py`, `drover/resources.py`, `drover/usage.py`, `drover/wsl_cache_reclaim.py`, `drover/seat_paths.py`, `drover/disk_schedule.py`, `drover/disk_schedule_steps.py`, `tests/seam_exceptions.json`, `tests/test_compaction.py`, `tests/test_disk_schedule.py`, the tests passing `run=` or `runner=` to them

**Interfaces:**
- Consumes: `process.run`.
- Production seams stay in their own shape, in slice 4 (`origin`'s `git=`, `pane_close:234`, `placement.add_worktree`'s `error=`). This task removes only `run=` parameters that exist for tests.

- [ ] **Step 1: Red.** Delete their R1 rows. For each, restate one test with `FakeCommands.patch(process)` and drop the `run=` argument. Add:
  ```python
  # tests/test_compaction.py
  def test_compaction_shutdown_literal_runs_in_the_offline_phase(self): ...   # 138, 159, compaction_vhd:48
  # tests/test_disk_schedule.py
  def test_schedule_install_times_out_at_600_seconds_and_reports_failed(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams tests.test_compaction tests.test_disk_schedule tests.test_herdr_client tests.test_usage tests.test_wsl_cache_reclaim`. `test_seams` must name them, and the restated tests must fail with `TypeError` or `unused command expectations`.
- [ ] **Step 3: Migrate.** Remove those parameters, every test argument with them, and each module's `import subprocess` when only exception types remain. disk_schedule_steps:64 calls `process.list2cmdline`. Commands with no timeout take `LONG` as a local `600.0` here and in Task 19, because `git_client.LONG` does not exist before Task 22.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_compaction tests.test_compaction_hub tests.test_pane_compaction_job tests.test_disk_schedule tests.test_disk_fix6 tests.test_disk_linux_schedule tests.test_herdr_client tests.test_launch_runner tests.test_usage tests.test_wsl_cache_reclaim tests.test_seat_paths_contract tests.test_seats tests.test_resources tests.test_pane_launch_files"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Test-only run parameters are gone, and one process-level fake covers every remaining spawn."

### Task 19: Hub callers

**Files:**
- Modify: `hub/__main__.py`, `hub/auth.py`, `hub/runtime.py`, `hub/task_views.py`, `hub/watchdog.py`, `tests/test_hub_runtime.py`, `tests/test_hub_watchdog.py`, `tests/seam_exceptions.json`, hub tests patching these

**Interfaces:**
- Consumes: `process.run`.
- `hub/watchdog:27`, the keep-alive, stays outside.
- `hub/runtime` is frozen (ruling 15): `tests/test_hub_runtime_paths.py:229` forbids importing `drover`. It keeps `subprocess`, its `NO_WINDOW` and `_git`, with its R1 and R3 rows. `_git` gains `timeout=600.0` in place, section 5.1's `LONG`, a guess. A timeout raises `TimeoutExpired`, a `SubprocessError`, which the handlers at :360, :516 and :535 hold; any other caller fails as a missing git does today.

- [ ] **Step 1: Red.** Delete R1 rows for `hub/__main__`, `hub/auth`, `hub/task_views`; `hub/watchdog` and `hub/runtime` keep theirs, and `hub/runtime`'s R1 and R3 reasons now state the frozen bootstrap. Restate fakes. Add to `tests/test_hub_runtime.py`: `test_hub_git_read_takes_600_seconds`, faked at `hub.runtime`'s own `subprocess`. Add to `tests/test_hub_watchdog.py`: `test_watchdog_17_runs_through_process`.
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams tests.test_hub_runtime tests.test_hub_watchdog`. Must name the three modules.
- [ ] **Step 3: Migrate** the Hub row of section 5.1. `hub/auth:28` keeps locale decoding. `hub/runtime.py` stays at exactly 612 lines, its `OVER` pin: the timeout goes on the existing line.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_hub_runtime tests.test_hub_runtime_integrity tests.test_hub_runtime_paths tests.test_hub_watchdog tests.test_task_views tests.test_authority_http tests.test_web_perf"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 5: Commit**: "The hub's short-lived commands now run through process, and the watchdog's keep-alive stays outside by rule."

### Task 20: `wsl_control` moves; `NO_WINDOW` has one home

**Files:**
- Modify: `drover/process.py`, `drover/placement_exec.py`, `drover/placement.py`, `drover/placement_host.py`, `drover/compaction.py`, `drover/disk_runtime.py`, `drover/pane_files.py`, `drover/wsl_cache_reclaim.py`, `hub/watchdog.py`, `drover/pane_status_hook.py` (no change expected, verify only), `tests/test_process.py`, `tests/test_compaction_hold.py`, `tests/test_compaction_hub.py`, `tests/test_disk_fix6.py`, `tests/test_disk_worker_status.py`, `tests/test_herdr_client.py`, `tests/test_pane_launch_files.py`, `tests/test_runner_detach.py`, `tests/test_wsl_cache_reclaim.py`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`

**Interfaces:**
- Produces: `process.wsl_argv(args: Sequence[str]) -> list[str]`, today's `wsl_control` with its hold check, raising `WslOffline` or `CommandTooLong` at build; `process.CREATE_NO_WINDOW`, which `placement_exec` imports.

- [ ] **Step 1: Failing tests**, `tests/test_process.py`:
  ```python
  def test_wsl_argv_refuses_over_8_kb(self): ...
  def test_wsl_argv_raises_wsl_offline_in_the_offline_phase(self): ...
  def test_run_makes_no_hold_check(self): ...      # literal ['wsl.exe', '--shutdown'] runs while phase='offline'
  def test_watchdog_keep_alive_is_refused_offline_at_build(self): ...      # hub/watchdog:27 through WSL.command
  ```
  Add `drover.placement_exec.wsl_control` to `qualified_names`.
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_process tests.test_architecture`. Must fail with `AttributeError` for `wsl_argv` and name `wsl_control`.
- [ ] **Step 3: Implement.** Move `wsl_control`; `placement_exec`'s `compaction_hold` import goes. `process` gains its `compaction_hold` import, and `compaction_hold` already reaches `durable`, so no cycle. Every caller takes `wsl_argv`: compaction.py:123, :129; disk_runtime.py:73, :91; pane_files.py:17, :48, :61; placement.py:31; placement_exec.py:104; placement_host.py:21, :131, :143, :335; wsl_cache_reclaim.py:65, :71; hub/watchdog.py:11, :17; and the tests patching it. `placement_exec.py:29,:41` definitions go: `spawn_worker` and `detach` (:83, :131) read `process.CREATE_NO_WINDOW`, and `placement.py:28` stops re-exporting both names. `hub/watchdog.py:28`'s kept `Popen` reads `process.NO_WINDOW`. `hub/runtime` keeps its own (ruling 15). `pane_status_hook` keeps its literal: standalone.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_process tests.test_placement tests.test_wsl_worker_argv tests.test_runner_detach tests.test_hub_watchdog tests.test_compaction_hold tests.test_compaction tests.test_compaction_hub tests.test_disk_fix6 tests.test_disk_worker_status tests.test_herdr_client tests.test_pane_launch_files tests.test_wsl_cache_reclaim tests.test_pane_status_hook"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Slice exit.** Every other short-lived command runs through `process`; the flag is defined only in `process` and the frozen `hub/runtime`, and the standalone hook keeps its literal; `placement_exec` and `hub/watchdog` read `process`'s; trace check prints nothing. Commit, with the slice report: "The WSL argv check now lives in process, so the application has one console flag and the compaction hold left placement."

### Task 21: Checkpoint after slice 3b

**Files:**
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-checkpoint-3b.md`
- Modify: `docs/superpowers/reviews/2026-11-02-rearchitecture-test-times.md`

Runs the Checkpoint protocol with `LAST_CP` as Task 7's report commit, covering slices 1, 2, 3a, 3b.

- [ ] **Step 1: Run the protocol.** Update the census for every module touched. Targets: the row after slice 3b.
- [ ] **Step 2: Reviewer focus.**
  - Completeness: sections 5.1, 5.3, 5.4, every row of the proof table, ruling 1's compare list in section 7.3, Review Focus 1, 2, 3, 5.
  - Scope: section 10 rows 1 to 3b; `tests/placement_trace.json` changed only in `kwargs`; `tests/disk_decision_trace.json` changed only in slice 1, and each row is in that report; `entries`, `registered=`, `write_json_atomic`, `wsl_control` and 12 sites' missing console flags are gone.
  - Efficiency hot paths: one listing per repo per stage; one `prove` per removal; `durable` reading no clock on a landing write; `Status.routine`'s fsync-free path; `process.run`'s overhead over `subprocess.run`, as spec section 5.1's cost line states it since the tree kill.
- [ ] **Step 3: Fix findings**, by the protocol, then record `Task 21: complete`.
- [ ] **Step 4: Commit** both reports: "The slice 3b checkpoint records its test times and its three reviews."

## Slice 4: `git_client`

Tasks 24 to 27 share one rule, from ruling 12, widened by 2M2: every handler around each migrated call is read, `except OSError` and domain handlers alike, and recorded in the slice report as `file:line, what it catches, what now reaches it, change`. The section 5.2 table is what the review found, not proof of a complete list.

### Task 22: `git_client`

**Files:**
- Create: `drover/git_client.py`, `tests/test_git_client.py`, `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-4.md`
- Modify: `tests/test_seams.py`

**Interfaces:**
- Consumes: `process.run`; `placement_git._git_command`, `request_git`, `git_spawns`, `git_spawn_lock`.
- Produces, as section 5.2: `Answer`; `Why`; `GitReply`; `GitDone`; `GitFailed(OSError)` with `args_, why, code, err, out`; `QUICK, NORMAL, FETCH, LONG = 30.0, 60.0, 120.0, 600.0`; `ask(repo, *args, timeout_s, no, env=None, side=None, sink=None) -> GitReply`; `out(repo, *args, timeout_s, env=None, side=None, error=None) -> str`; `mutate(repo, *args, timeout_s, env=None, side=None) -> GitDone`; `argv(repo, args, side=None) -> list[str]`; `request_git`, the same object as `placement_git`'s until Task 28.

- [ ] **Step 1: Failing tests**, `tests/test_git_client.py`, default tier:
  ```python
  def test_answer_refuses_truth_testing(self):
      for use in (bool, lambda a: not a): self.assertRaises(TypeError, use, Answer.UNKNOWN)
  def test_ask_exit_zero_is_yes_listed_exit_is_no_other_is_unknown(self): ...
  def test_no_has_no_default(self): ...                    # TypeError
  def test_timeout_s_has_no_default_on_any_verb(self): ...
  def test_why_is_set_exactly_when_unknown(self): ...      # exit, timeout, unavailable
  def test_missing_git_is_unavailable_not_127(self): ...
  def test_wsl_offline_at_build_is_unavailable(self): ...
  def test_bad_git_pointer_is_git_failed_unavailable(self): ...
  def test_out_returns_stdout_and_raises_git_failed_with_why(self): ...
  def test_error_keeps_a_domain_type_with_todays_message(self): ...
  def test_git_failed_is_an_oserror_and_str_holds_no_stderr(self):
      self.assertNotIn('https://token@host', str(GitFailed(('fetch',), 'exit', 1, 'https://token@host', '')))
  def test_mutate_reports_ok_and_why_and_has_no_answer(self): ...
  def test_nothing_retries(self): ...
  def test_no_124_or_127_reaches_any_caller(self): ...
  def test_spawn_counts_stay_for_the_hub_performance_log(self): ...
  def test_argv_equals_the_journals_restore_argv(self): ...
  ```
  In `tests/test_seams.py`: the live R5 rule for `git_client`, and `test_r5_placement_git_imports_no_git_client_but_its_stated_rows`. The rule is that `placement_git` imports no `git_client`, so each import is a finding, and only function-level rows may stand in the manifest. Task 27 adds them.
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_git_client` fails with `ModuleNotFoundError` for `drover.git_client`.
- [ ] **Step 3: Implement.** Ordering: `placement_git` for argv and env, `process.run`, count. Path mapping stays in `placement_git`. `placement_git.git` is untouched until Task 28, and `placement_git` cannot import `git_client` at load. So both routes count in one place: `git_client` imports `request_git` and increments `placement_git.git_spawns` under `placement_git.git_spawn_lock`. Task 27 repoints `hub/app`, and Task 28 moves the definitions.
- [ ] **Step 4: Green.**
  ```bash
  DROVER_TESTS=integration $PY -m unittest tests.test_git_client tests.test_placement $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "One git client now answers yes, no or unknown, and a timeout means one thing to every caller."

### Task 23: `git_worktrees` swaps; four listings move

**Files:**
- Modify: `drover/git_worktrees.py`, `drover/placement.py`, `hub/task_views.py`, `drover/cleanup.py`, `drover/pipeline.py`, `tests/test_git_worktrees.py`, `tests/test_task_views.py`, `tests/__init__.py`, `tests/test_guards.py`, `tests/seam_exceptions.json`

**Interfaces:**
- Consumes: `git_client.out`.
- `git_worktrees` now imports `git_client`, and R5's temporary row goes. `listing`'s required `timeout_s` is `NORMAL` for `placement.unlock_worktree`, `LONG` for `pipeline.inventory` and `cleanup.checked_out_anywhere`, and 1 s for `hub/task_views.probe`, as today. A failed tip now raises `GitFailed`.

- [ ] **Step 1: Red.** Delete R5's temporary row and R8's rows for cleanup:49, pipeline:106, placement:153, hub/task_views:120. Restate the expected timeouts in `tests/test_git_worktrees.py`, and its tip test as `test_tip_failure_raises_git_failure`. Add:
  ```python
  def test_inventory_returns_empty_on_a_failed_listing(self): ...       # pipeline.inventory, today's behaviour
  def test_probe_keeps_its_one_second_timeout(self): ...                # hub/task_views.probe
  def test_checked_out_anywhere_and_unlock_worktree_use_listing(self): ...
  def test_inventory_and_checked_out_anywhere_list_on_600_seconds(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams tests.test_git_worktrees`. Must name the four literals and the R5 row, and the tip test must fail on its type.
- [ ] **Step 3: Migrate.** `origin.fast_forward` (:27) keeps its literal: R8's one row. Behaviour is unchanged at all four callers; `native` normalises as Review 3 checked. If `tests.test_task_views` has no real spawn left, remove it from `tests.IMPURE` and lower the ceiling.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_git_worktrees tests.test_registration_trace tests.test_disk_decision_trace tests.test_task_views tests.test_pipeline_worktrees tests.test_garden $(mods 'unlock_worktree|checked_out_anywhere|inventory\(')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Every worktree listing but origin's landing read now goes through git_worktrees on git_client."

### Task 24: `pipeline`, `replan`, `landing`, `landing_batch`

**Files:**
- Modify: `drover/pipeline.py`, `drover/replan.py`, `drover/landing.py`, `drover/landing_batch.py`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`, `tests/test_landing.py`, `tests/test_replan_batches.py`, `tests/test_pipeline_worktrees.py`, tests naming `pipeline.git`, `landing_batch`'s calls, `timed_git`, rc 124

**Interfaces:**
- Consumes: `ask`, `out`, `mutate`, `LONG`.
- `landing_batch`'s own calls and `landing.py`'s migrate here, but `landing_batch.git` stays for `landing_control` (:10, :18, :43, :44, :174, :204) until Task 25. `contained` returns `Answer`. `pipeline.Repository` keeps its methods. `error=` gives `WorktreeRefused` with today's stderr tail, and `CIError`.

- [ ] **Step 1: Red.** Add `drover.pipeline.git`, `drover.landing.timed_git` to `qualified_names`. Delete R1 rows for `landing` and `pipeline`. Tests that assert rc 124, or a `TimeoutExpired` from `timed_git`, **die**, each replaced in place by an `Answer` assertion. Add:
  ```python
  # tests/test_landing.py
  def test_landing_256_raises_as_not_contained_when_unknown(self): ...
  # tests/test_replan_batches.py
  def test_cherry_pick_timeout_raises_ci_error_and_neither_drops_the_task_nor_resets(self): ...
  def test_cherry_pick_conflict_is_still_a_conflict(self): ...
  # tests/test_pipeline_worktrees.py
  def test_every_check_true_site_raises_worktree_refused_with_stderr_tail(self): ...   # pipeline :72 :89 :92 :142 :145 :146 :151 :155-157, replan :49
  def test_reads_without_a_timeout_take_600_seconds(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_seams tests.test_landing tests.test_replan_batches tests.test_pipeline_worktrees`. Must name the two symbols and the two R1 modules, and the new tests must fail on rc 124.
- [ ] **Step 3: Migrate** by the section 5.2 table and section 5.1's no-timeout rows. `pipeline` is at 491: it must shrink. Record each handler.
- [ ] **Step 4: Green.**
  ```bash
  M="$(mods 'pipeline\.git|landing_batch|timed_git|landing\.contained|pipeline_fixture|landing_fixture|replan\b') tests.test_landing tests.test_pipeline_worktrees tests.test_scope_base"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Pipeline, replan and landing now ask git through git_client, and domain handlers still receive their own types."

### Task 25: `origin` and `ControlledGit`

**Files:**
- Modify: `drover/origin.py`, `drover/landing_control.py`, `drover/landing_batch.py`, `drover/landing.py`, `drover/placement.py` (`add_worktree`), `drover/pane_close.py`, `tests/test_origin_sync.py`, `tests/test_landing_recovery.py`, `tests/__init__.py`, `tests/test_guards.py`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`, tests naming `landing_batch.git`

**Interfaces:**
- Produces: `origin` functions take `git=git_client`, an object with `ask`, `out`, `mutate`; `landing_control.ControlledGit(conn, epoch)` with those three, guarding `merge`, `update-ref` and `push` by `args[0]`; `pane_close:234` takes a function shaped like `ask`; `placement.add_worktree(..., error=, timeout_s=LONG)`, and the registration repair passes `NORMAL` (spec Appendix C, slice 4).

- [ ] **Step 1: Red.** Add `drover.origin.git`, `drover.landing_batch.git` and `drover.landing_control._controlled_git` to `qualified_names`. Delete R1 rows for `origin` and `landing_batch`. Restate `tests/test_origin_sync.py` around a `FakeGit` object with the three methods, replacing real repos where git is only setup. Add:
  ```python
  def test_cas_on_main_stays_a_mutate(self): ...
  def test_non_zero_update_ref_raises_main_moved(self): ...
  def test_timed_out_merge_raises_sync_error_not_main_moved(self): ...
  def test_check_true_reads_and_push_main_raise_sync_error_with_no_stderr(self): ...
  def test_controlled_git_guards_merge_update_ref_push_only(self): ...   # tests/test_landing_recovery.py
  def test_add_worktree_keeps_no_60_second_limit(self): ...              # LONG, its two callers
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_seams tests.test_origin_sync tests.test_landing_recovery`. Must name the three symbols and the two R1 modules, with `TypeError` for `git=`.
- [ ] **Step 3: Migrate.** `landing_control`'s own reads (:43, :44, :174, :204) leave `landing_batch.git` for `git_client`, `LONG` where they have no timeout and `CIError` by `error=`, so `landing_batch.git` goes. landing.py:15 stops importing `_controlled_git`. `landing.py:225`, `hub/store_ui.py:303`, `landing_control.py:52,:192` and `pipeline.py:421` still catch `SyncError`: confirm and record. If `tests.test_origin_sync` is now pure, take it out of `tests.IMPURE` and lower the ceiling.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_origin_sync tests.test_landing_recovery tests.test_landing tests.test_landing_repository tests.test_landing_diagnosis tests.test_pane_close tests.test_pane_close_safety tests.test_disk_worktree_add tests.test_wsl_worktree"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Origin takes a git object, so the landing control guard stays inside it and the compare-and-swap on main stays a mutation."

### Task 26: `ci`, `cleanup`, disk git, small callers

**Files:**
- Modify: `drover/ci.py`, `drover/cleanup.py`, `drover/disk_worktrees.py`, `drover/disk_paths.py`, `drover/disk_registrations.py`, `drover/dispatch_hold.py`, `drover/task_access.py`, `drover/decisions.py`, `drover/brief.py`, `drover/context_files.py`, `drover/repos.py`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`, `tests/test_disk_fresh_proof.py`, `tests/test_ci_policy.py`, `tests/test_decisions.py`, `tests/test_task_access.py`, `tests/test_dispatch_hold.py`, tests naming these functions

**Interfaces:**
- Consumes: `ask`, `out`, `mutate`.
- `disk_worktrees.git` deleted: `error=` gives `ValueError`, so disk_registrations.py:66 and the disk tuples stay. `repository_config` uses `git_client` here, still in `disk_paths`, and catches `GitFailed` as an `OSError` for its fail-closed `True`.

- [ ] **Step 1: Red.** Add `drover.ci.git`, `drover.cleanup.git`, `drover.disk_worktrees.git`, and the private names `drover.dispatch_hold._git`, `drover.task_access._git`, `drover.decisions._git` to `qualified_names`. Delete R1's `dispatch_hold` row and R3's `decisions._git` row. Add:
  ```python
  # tests/test_disk_fresh_proof.py
  def test_git_failure_after_unlock_restores_the_marker_and_journals_failed(self): ...   # disk_worktrees.py:381, ruling 12
  # tests/test_ci_policy.py
  def test_head_dirty_contains_changed_paths_raise_ci_error_with_todays_type(self): ...
  # tests/test_decisions.py
  def test_git_failure_is_decision_error_at_config_475_and_decisions_88(self): ...
  # tests/test_task_access.py
  def test_lines_58_63_still_fall_through_to_the_remote_ref(self): ...
  # tests/test_dispatch_hold.py
  def test_unknown_is_not_read_as_no(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_seams tests.test_disk_fresh_proof tests.test_ci_policy tests.test_decisions tests.test_task_access tests.test_dispatch_hold`. Must name the six symbols and both rows' modules.
- [ ] **Step 3: Migrate** by the section 5.2 table. `cleanup.git` and `_git_ok` take `LONG`; cleanup's own reads and ref changes run with `GIT_OPTIONAL_LOCKS=0`, and its worktree listing goes through `git_worktrees.listing` without it (spec Appendix C, slice 4). `ci.py:119` stays an R3 row. `ci`, `disk_worktrees` and `disk_paths` must shrink.
- [ ] **Step 4: Green.**
  ```bash
  M="$(mods 'ci\.git|cleanup\.git|_git_ok|disk_worktrees\.git|dispatch_hold|task_access|decisions\._git|context_files|ci_fixture') tests.test_ci_policy tests.test_disk_decision_trace tests.test_disk_fresh_proof tests.test_disk_removal tests.test_disk_registrations"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 5: Commit**: "CI, cleanup, the disk stages and the small git callers now use git_client with their domain errors kept."

### Task 27: `outside_land`, ledger, hub, placement internals

**Files:**
- Modify: `drover/outside_land.py`, `drover/ledger/ops_outside.py`, `hub/task_views.py`, `hub/app.py`, `drover/placement.py`, `drover/placement_git.py`, `tests/test_outside_land.py`, `tests/architecture_import_allowlist.json`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`

**Interfaces:**
- `outside_land._out` stays three lines over `ask`. `GitUnknown` deleted: callers catch `GitFailed`. `hub/runtime._git` stays as it is, frozen (ruling 15). `hub/task_views.git` deleted. `placement_git.reachable` and `worktree_metadata` import `git_client` inside the function: a stated R5 row.

- [ ] **Step 1: Red.** Add `drover.outside_land.GitUnknown` to `qualified_names`. `hub/task_views.git` is a nested function at :112, so no removed-symbol row can see it: delete its R3 row instead. Delete R1 rows for `outside_land` and `ledger/ops_outside`. Add:
  ```python
  # tests/test_outside_land.py
  def test_459_timeout_takes_stuck_and_resume(self): ...          # e.why == 'timeout'
  def test_459_any_other_failure_logs_as_464(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest $GUARDS tests.test_outside_land`. `test_architecture` must name `GitUnknown`, and `test_seams` must name `hub/task_views.py`, `outside_land` and `ledger/ops_outside`.
- [ ] **Step 3: Migrate.** `ledger/ops_outside` importing `drover.placement` swaps one allowlist row for `drover.git_client`: same row count. `hub/app.py` imports `request_git` from `git_client`. `add_worktree`, `unlock_worktree`, `reachable`, `worktree_metadata` use `git_client`, and R5's two function-level `placement_git` rows land with those imports.
- [ ] **Step 4: Green.**
  ```bash
  M="$(mods 'outside_land|GitUnknown|ops_outside') tests.test_hub_runtime tests.test_hub_runtime_paths tests.test_hub_runtime_integrity tests.test_task_views tests.test_web_perf tests.test_placement"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Outside landing, the ledger, the hub and placement's own git calls now go through git_client."

### Task 28: `placement.git` goes; R3 final; conversions

**Files:**
- Modify: `drover/placement_git.py`, `drover/placement.py`, `drover/git_client.py`, `tests/test_git_client.py`, `tests/test_web_perf.py`, `tests/test_guards.py`, `tests/__init__.py`, `tests/test_seams.py`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`, every test naming `placement.git`

**Interfaces:**
- `placement_git.git` and `placement.git` are deleted; `GIT_TIMED_OUT` deleted. `request_git`, `git_spawns` and `git_spawn_lock` move to `git_client`, and tests/test_web_perf.py:281-283 reads them there. Later tasks reach git only through `git_client`.

- [ ] **Step 1: Red.** Add `drover.placement_git.git`, `drover.placement.git` and the remaining private functions of section 10's row 4 to `qualified_names`. Delete R3's and R1's rows that this makes false, `placement_git`'s R1 row among them. Rewrite `tests/test_guards.py`'s `test_patched_git_preserves_missing_executable_result` against `git_client`: the missing-executable case asserts `Answer.UNKNOWN`, `why='unavailable'`, and no rc 127. It now says what Task 22's `test_missing_git_is_unavailable_not_127` says, so that test goes in this commit.
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_seams tests.test_guards`. Must name `placement.git`.
- [ ] **Step 3: Delete** them, and move every remaining test fake of `placement.git` to process-level fakes: the 53 test files of section 11 not already moved. Tests asserting rc 124 or 127 that remain die with their `Answer` replacement.
- [ ] **Step 4: Convert.** From Task 1's candidates, for each module where git is setup and not the point: `test_task_access`, `test_disk_branch`, `test_disk_policy`, `test_disk_ownership`, `test_disk_caches`, `test_hub_runtime_paths`. Replace real git with `FakeCommands.patch(process)` or `pipeline.Repository` facts, in place, one commit per module. Each commit removes the module from `tests.IMPURE`, or converts `@integration` tests, and lowers the constant in that commit. A test whose point is real git stays: name it in the report. `test_hub_runtime_paths` fakes at `hub.runtime`, which is frozen, and stays in `tests.IMPURE`: its frozen-module test spawns real Python on purpose. Do not add a parallel test.
- [ ] **Step 5: Green.**
  ```bash
  M="$(mods 'placement\.git|placement_git\.git|GIT_TIMED_OUT') tests.test_git_client tests.test_web_perf tests.test_process tests.test_disk_decision_trace tests.test_placement"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $M tests.test_guards
  ```
- [ ] **Step 6: Slice exit.** All git but the frozen `hub/runtime._git` goes through `git_client`; R3 final with its `Answer.NO` check; R8 at one row; R5's two `placement_git` rows in; R1 shrunk; slice 4's removed symbols added; handler list complete in the report; trace check prints nothing. Commit: "The per-module git functions are gone, and placement.git with them, so one git client answers for everything but the frozen bootstrap."

### Task 29: Checkpoint after slice 4

**Files:**
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-checkpoint-4.md`
- Modify: `docs/superpowers/reviews/2026-11-02-rearchitecture-test-times.md`

Runs the Checkpoint protocol with `LAST_CP` as Task 21's report commit.

- [ ] **Step 1: Run the protocol.** Census for every converted module. Targets: the row after slice 4.
- [ ] **Step 2: Reviewer focus.**
  - Completeness: section 5.2 invariants and every row of its table; section 5.1's no-timeout table; every entry of the handler list has a reason, and `disk_worktrees.py:381` has its test.
  - Scope: section 10 row 4; R3's three rows, ci:119, pane_files:80 and `hub/runtime._git`, and no others; R8 at origin:27; allowlist row count not grown; no `Answer` truth-tested; no `timeout_s` missing.
  - Efficiency hot paths: `ask` over `process.run`, argv build once; the hub's performance-log counts unchanged; `pipeline.Repository`'s reads; no read gained a spawn. Also which real-git tests remain, and why.
- [ ] **Step 3: Fix findings**, by the protocol, then record `Task 29: complete`.
- [ ] **Step 4: Commit** both reports: "The slice 4 checkpoint records its test times and its three reviews."

## Slice 5: the stage run

Commits inside this slice keep the suite green through one temporary bridge, `disk_side._bridge`, which Task 33 adds and Task 35 deletes. Every commit is green, so a task that moves trace rows re-records in its own commit, by Task 10 Step 4's commands, comparing against `SLICE_BASE`. The slice report lists the cumulative changed rows.

### Task 30: Prove partial delivery; `StageRequest` and `StageClock`

**Files:**
- Create: `drover/disk_stage.py`, `tests/test_disk_stage.py`, `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-5.md`
- Modify: `tests/test_seams.py`

**Interfaces:**
- Produces, as section 6.1: `StageRequest` with `to_wire`, `from_wire`, `for_side(side, convert)`, `decide_view`; `StageClock(timeout_s, *, clock=time.monotonic, exempt=False)` with `SHARE=0.8`, `FLOOR_S=10.0`, `MUTATION_STEPS=5`, `late`, `left`, `remaining`, `patience`, `mutation_cap`, `can_mutate`, `within`.

- [ ] **Step 1: Prove partial delivery by hand.** A test stage in a temp tree writes one flushed line per second through real `wsl.exe`, is killed mid-run, and the coordinator must hold the flushed lines. Commit nothing from this: a committed test would need an `IMPURE` row or an integration mark (ruling 6). Record the result in the slice report. If rows are lost, report BLOCKED before any stage moves: the design's fallback is the file route, behind the same `checkpoint`, and the lead must add its tasks.
- [ ] **Step 2: Failing tests**, default tier, `clock=` faked:
  ```python
  def test_late_at_the_80_percent_line_never_when_exempt(self):
      self.assertEqual([StageClock(100, clock=at(t)).late() for t in (79.9, 80)], [False, True])
  def test_left_and_remaining_never_fall_below_zero(self): ...
  def test_exempt_remaining_is_left(self): ...
  def test_patience_table(self):       # worker timeout 1800, cap 60, by elapsed
      for elapsed, want in ((0, 60), (1439, 10), (1795, 5), (1800, 1)): self.assertEqual(clock_at(elapsed).patience(60), want)
  def test_mutation_cap_is_fixed_for_the_run(self):
      self.assertEqual(StageClock(1800).mutation_cap(), 60); self.assertEqual(StageClock(100).mutation_cap(), 4)
  def test_can_mutate_needs_five_capped_steps_left_and_is_true_when_exempt(self): ...
  def test_small_worker_timeout_stays_coherent(self):                  # Review Focus 4
      clock = StageClock(11); self.assertAlmostEqual(clock.mutation_cap(), 0.44); self.assertTrue(clock.can_mutate())
  def test_within_gives_a_budget_under_the_stage_clock(self): ...
  def test_wire_keys_are_todays(self):
      self.assertLessEqual({'_worker_id', '_inventory_id', 'paths', 'state_dir', 'stage', 'side', 'apply'}, set(request.to_wire()))
  def test_from_wire_round_trips(self): ...
  def test_for_side_matches_todays_translation_with_spaces_and_non_ascii(self): ...    # disk_runtime.py:39-59
  def test_decide_view_is_a_plain_dict_with_approved_path(self): ...
  ```
  In `tests/test_seams.py`: the `disk_stage` direction rule, imports only `placement_paths`.
- [ ] **Step 3: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_disk_stage` fails with `ModuleNotFoundError` for `drover.disk_stage`.
- [ ] **Step 4: Implement** per section 6.1's clock rules. `patience(cap)` is `max(1, min(cap, max(FLOOR_S, remaining()), left()))`, and `mutation_cap()` is `min(60, margin / MUTATION_STEPS)`.
- [ ] **Step 5: Green.**
  ```bash
  DROVER_TESTS=integration $PY -m unittest tests.test_disk_stage $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 6: Commit**, with the slice report: "A typed stage request and one stage clock now state what a stage is asked and how long it has."

### Task 31: `StageRun` and `read_rows`

**Files:**
- Modify: `drover/disk_stage.py`, `tests/test_disk_stage.py`

**Interfaces:**
- Produces, as section 6.1: `StageRun` with `request, clock, evidence, scope_errors`, `stopped`, `reclaimed_bytes`, `add`, `error`, `reclaimed`, `checkpoint`, `stop(reason, machine, left, later)`, `finish(**extra)`; `StageAbandoned(BaseException)`; `read_rows(output: bytes) -> StageAnswer`; `StageAnswer`. `StageRun.__init__(request, clock, *, out: BinaryIO, root_of: Callable[[dict], str], limit: int)`: `root_of` is the step `disk_side` passes in, built from `compact`, and `limit` is `disk_inventory.LIMIT`, passed the same way, because `disk_stage` may import neither. `stop`'s `machine` may be None, for a stage with no repo in hand, and the row's `path` is then `request.drive['path']`. `read_rows` has no exit status: it sets `complete` from the `end` line, and `cleanup_call` clears it on a non-zero exit (Task 33).
- Wire: `{"v":1,"row":{...}}`, `"error"`, `"more"`, `"reclaimed"`, `"end"`, one per line.

- [ ] **Step 1: Failing tests**, with a `BytesIO` for stdout:
  ```python
  def test_a_stage_killed_mid_run_keeps_its_settled_rows(self):
      answer = read_rows(b''.join(lines[:2]) + b'{"v":1,"ro'); self.assertEqual((len(answer.items), answer.complete, answer.partial), (2, False, True))
  def test_a_cut_inside_a_multibyte_character_is_a_partial_last_line(self): ...
  def test_a_malformed_complete_line_raises_value_error(self): ...
  def test_a_line_with_no_v_key_raises_value_error(self): ...          # the old single-object answer
  def test_nothing_at_all_is_not_complete(self): ...
  def test_add_is_the_only_way_in_and_checkpoint_sends_only_settled_rows(self): ...
  def test_add_caps_groups_at_limit_and_tallies_more(self): ...
  def test_read_rows_aggregates_by_stamped_root(self): ...
  def test_a_failed_checkpoint_raises_stage_abandoned_past_every_except(self):
      with self.assertRaises(StageAbandoned):
          try: run.checkpoint()
          except Exception: self.fail('held by except Exception')
  def test_stop_writes_one_row_and_sets_partial_and_stopped(self): ...
  def test_stop_with_no_machine_writes_the_drive_path(self): ...
  def test_reclaimed_uses_end_when_complete_and_last_line_otherwise(self): ...
  def test_end_carries_peak_and_budget(self): ...
  ```
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_disk_stage`. Must fail with `AttributeError`/`ImportError` for `StageRun`.
- [ ] **Step 3: Implement.** `add` caps as it goes (`dirty`, `ignored`, `excluded` to `limit`; at most `limit` whole rows per `compact` group). Flush per `checkpoint`. A row is whole on one line, with `ensure_ascii=True`, so a stage's non-UTF-8 path survives.
- [ ] **Step 4: Green.** Same commands as Task 30.
- [ ] **Step 5: Commit**: "A stage run now sends each settled row as a line, so a killed worker's rows survive it."

### Task 32: Plain arguments for placement and inventory

**Files:**
- Modify: `drover/placement.py`, `drover/placement_proof.py`, `drover/disk_inventory.py`, `drover/disk_paths.py`, `drover/disk_stage.py`, `drover/disk_temp.py`, `drover/disk_worktrees.py`, `drover/disk_budget.py`, `drover/disk_registrations.py`, `drover/pane_close.py`, `drover/disk_cleanup.py`, `drover/disk_side.py`, `tests/architecture_removed_symbols.json`, `tests/test_disk_scope.py`, tests of these

**Interfaces:**
- Produces, as section 6.1: `cleanup_contains(drive_path, path)`; `cleanup_scope(drive_path, paths) -> (scoped_paths, scope_errors)`; `cleanup_processes(*, paths=None, state_dir=None, inventory_id=None, stage=None, drive=None, budget=False, clear_ignored=False, proc_root=None, cim=None, windows=None)` returning its diagnostics; `cleanup_windows_proof(*, paths, state_dir, budget)`, a plan addition to section 6.1, because `_CleanupProcesses` loses the dict; `Inventory(now, side, drive_path, state_dir, inventory_id, ...)`; `compact(items, policy, repos)`; `disk_stage.item`, `result`, `within`, moved. `item` keeps its unused `request` until Task 35.

- [ ] **Step 1: Red.** Add `drover.disk_paths.item`, `.result`, `.within` to `qualified_names`. Add:
  ```python
  # tests/test_disk_scope.py
  def test_cleanup_scope_takes_plain_arguments_and_returns_errors(self): ...
  def test_cleanup_processes_cache_key_keeps_stage_and_drive(self): ...     # two stages do not share a snapshot
  def test_cleanup_processes_budget_and_clear_ignored_change_what_counts(self): ...
  def test_cleanup_processes_returns_diagnostics_and_mutates_nothing(self): ...
  def test_cleanup_windows_proof_takes_plain_arguments(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_disk_scope`. Must name the three symbols, with `TypeError` on the plain call.
- [ ] **Step 3: Implement.** Change signatures and every caller, which unwrap the dict where they still hold one: disk_side.py:10, :12, :15, :20, which keeps `request['_process_diagnostics']` filled from the returned diagnostics until Task 33; pane_close.py:133, and disk_cleanup.py:119 through `cleanup_windows_proof`. Decisions do not move here: no trace change.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_scope tests.test_disk_decision_trace tests.test_disk_stage tests.test_placement tests.test_pane_close_safety $(mods 'cleanup_processes|cleanup_scope|cleanup_contains|Inventory\(|compact\(')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Placement and the inventory now take plain arguments, so neither needs the request dict."

### Task 33: `disk_evidence`, small; the wire; `cleanup_call`

**Files:**
- Create: `drover/disk_evidence.py`, `tests/test_disk_evidence.py`
- Modify: `drover/disk_side.py`, `drover/placement_worker.py`, `drover/disk_runtime.py`, `drover/disk_cleanup.py`, `tests/disk_trace.py`, `tests/disk_fixture.py`, `tests/placement_trace.py`, `tests/placement_trace.json`, `tests/test_disk_worker_status.py`, `tests/seam_exceptions.json`

**Interfaces:**
- Consumes: `StageRun`, `StageClock`, `Listings`.
- Produces: `StageEvidence(request, clock, *, listings=git_worktrees.Listings, prove=git_worktrees.prove, processes=placement.cleanup_processes)`, holding only `inventory` (read-only), `listed`, `every_registered`, `row`, `took`, `repaired`, `processes()`, `process_rows()`; `disk_side.serve(wire, *, out, clock_factory=StageClock) -> None`; `cleanup_call(side, request: StageRequest) -> dict`, same answer shape with `complete` and `partial`; `CleanupRuntime.call` takes a `StageRequest`.
- Temporary: `disk_side._bridge(run, stage_fn)` builds the old dict from `run` (`_inventory`, `_processes`, `_listings` included), calls the old stage, and `add`s its rows. Deleted in Task 35.

- [ ] **Step 1: Failing tests.**
  ```python
  # tests/test_disk_evidence.py
  def test_listings_take_patience_when_each_listing_starts(self):        # 3M3, worker timeout 1800: 240 at 1200 s elapsed, 10 at the line
  def test_processes_is_one_snapshot_and_process_rows_match_disk_side_21_32(self): ...
  def test_inventory_is_read_only(self): ...
  def test_listing_failure_is_raised_from_listed(self): ...
  # tests/test_disk_worker_status.py
  def test_serve_builds_the_clock_before_the_process_proof(self): ...      # section 3 item 4
  def test_timeout_returns_rows_plus_todays_error_row(self): ...
  def test_partial_answer_does_not_set_answer_so_evidence_branch_runs(self): ...
  def test_rows_are_saved_whole_in_their_own_key_of_worker_error(self): ...
  def test_unconfirmed_termination_still_raises_with_rows_saved(self): ...
  def test_nonzero_exit_with_an_end_line_keeps_rows_and_complete_false(self): ...   # and partial true
  def test_claim_keeps_pid_run_id_request_and_drops_completed_result(self): ...  # O9
  ```
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_disk_evidence tests.test_disk_worker_status`. Must fail with `ModuleNotFoundError` for `drover.disk_evidence`, and the worker tests on the old answer.
- [ ] **Step 3: Implement.** `serve`: clock, scope, evidence, proof, stage through a stage table replacing the `if` chain and the five function-level imports, whose R6 rows go in this commit, `finish`. `serve` stays the only name `placement_worker` imports, and the `__main__` block at disk_side.py:69-70 goes: `placement_worker` is the only entry. Replace `json.dump` at placement_worker.py:99 and the copy at :98. `cleanup_call`: `StageRequest.for_side` for :39-59, `read_rows` for the answer, then on a non-zero exit `complete` False and `partial` True; the termination proof is unchanged in meaning and order. `disk_cleanup` builds `StageRequest`s, `dataclasses.replace` in place of `dict(request, ...)`. `tests/disk_fixture.py`'s `runtime.call` deep-copies and adapts in one place. Repoint `disk_trace` through the new `serve`, reading rows back with `read_rows`. `StageEvidence`'s `processes=` default binds at definition and `serve` takes no evidence, so the harness patches `disk_side.StageEvidence` with `partial(StageEvidence, processes=fake)`.
- [ ] **Step 4: Re-record** `tests/placement_trace.json`: answers now come from lines, and the recorded `answers` fixtures are re-recorded in `tests/placement_trace.py`. Decisions must not change. The decision trace must still match without re-record: if it differs, stop and find out why.
- [ ] **Step 5: Green.**
  ```bash
  M="tests.test_disk_evidence tests.test_disk_worker_status tests.test_disk_stage tests.test_disk_decision_trace tests.test_placement $(mods 'cleanup_call|disk_side|runtime\(|_serve_stage')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 6: Commit**: "The stage worker now streams rows, and a timed-out worker's rows are kept with its termination proof unchanged."

### Task 34: Stages 4, 5 and the budget purge on the run

**Files:**
- Modify: `drover/disk_worktrees.py`, `drover/disk_paths.py`, `drover/disk_budget.py`, `drover/disk_side.py`, `tests/test_disk_stage.py`, `tests/test_disk_settled_deadline.py`, `tests/test_disk_budget.py`, `tests/test_disk_budget_folders.py`, `tests/disk_budget_fixture.py`, `tests/disk_decision_trace.json`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`

**Interfaces:**
- Consumes: `run.clock`, `run.stop`, `run.add`, `run.evidence`, `run.stopped`, `run.reclaimed_bytes`.
- Produces: `settled(run)`, `outputs(run)`, `purge(run)`, `disposable_rows(run)`, with `checkpoint` between removal units only.

- [ ] **Step 1: Red.** Add `drover.disk_budget.deadline`, `drover.disk_budget._clock`, `drover.disk_paths.deadline_row` to `qualified_names`. `test_disk_settled_deadline` and the `test_disk_budget*` deadline tests **keep their assertions** with a `StageClock(clock=...)` in place of the `_clock` patches (4 sites). Add:
  ```python
  # tests/test_disk_stage.py
  def test_stage_4_and_5_stamp_at_their_deadline_as_today(self): ...       # ruling 7: item, no error
  def test_past_the_line_before_planned_keeps_removal_deadline(self): ...  # can_mutate false, no journal row
  def test_exempt_approved_removal_is_never_cut_off(self): ...
  def test_checkpoint_never_runs_between_planned_and_completed(self): ...
  def test_purge_reads_stopped_not_out_items(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_disk_stage tests.test_disk_settled_deadline`. Must name the three symbols, and the settled tests must fail with `TypeError` on `settled(run)`.
- [ ] **Step 3: Implement.** Replace the five stop checks and `purge`'s scan of `out['items']`. `decide` receives `run.request.decide_view()`, so `dict(view, approved_path=None)` replaces `dict(request, approved_path=None)`. `removal_deadline` keeps, in `remove`, before `planned`. Delete the bridge's cases for stages 4 and 5. Rewrite `disposable_rows` so a candidate is handed unadded and the consumer adds it when decided (section 6.1). R6 falls by what this ends.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_stage tests.test_disk_settled_deadline tests.test_disk_decision_trace tests.test_disk_fresh_proof $(mods 'disk_budget|disposable_rows|settled\(|outputs\(|purge\(')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
  Before this, re-record as the slice header says: only rows this task moves may change, each towards keeping or a report row. The clock starts earlier, so stages stop sooner, and `can_mutate()` can keep a worktree before `planned`.
- [ ] **Step 5: Commit**: "Stages 4 and 5 and the budget purge now take their time from one clock and stop at one row."

### Task 35: Stages 2, 3 and 6 on the run; the bridge goes

**Files:**
- Modify: `drover/disk_temp.py`, `drover/disk_paths.py`, `drover/disk_side.py`, `drover/disk_inventory.py`, `drover/disk_stage.py`, `drover/disk_worktrees.py`, `drover/disk_registrations.py`, `tests/test_disk_temp_budget.py`, `tests/test_disk_stage.py`, `tests/test_disk_trim.py`, `tests/disk_decision_trace.json`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`

**Interfaces:**
- Produces: `temporary(run)`, `caches(run)`, `report(run)`; `trim(run)` in `disk_side`, stage 1, which until now ran inside `_serve` through the bridge; `_temp_facts`, `_temp_children`, `_temp_deadline` become locals, `within` the per-root budget.

- [ ] **Step 1: Failing tests.** Add `drover.disk_side._bridge` to `qualified_names`. `test_disk_temp_budget` keeps its assertions with a `StageClock`, and `test_disk_trim` is restated on `trim(run)`. Add to `tests/test_disk_stage.py`:
  ```python
  def test_stage_2_stops_at_the_line_with_temp_deadline(self): ...
  def test_stage_3_stops_at_the_line_with_cache_deadline(self): ...
  def test_stage_6_stops_at_the_line_with_report_deadline(self): ...
  def test_report_rows_are_built_before_add(self): ...       # no append then update or pop at disk_paths :442 :457 :465 :475
  def test_incomplete_budget_usage_never_leaves_the_worker(self): ...
  ```
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_disk_stage tests.test_disk_temp_budget tests.test_disk_trim`. Must fail on the three deadline reason codes, and name `_bridge`.
- [ ] **Step 3: Implement.** `trim(run)` joins the stage table, so the bridge carries nothing and goes. All 9 underscore keys are gone from the request. `item` drops its unused `request` at every caller in `disk_paths`, `disk_temp`, `disk_worktrees` and `disk_registrations`. R6 falls. Stage 6 gains a line and stages 2, 3 and 6 a clock, so re-record as the slice header says. `disk_stage` direction rule stays true: `item`, `result`, `within` need only `placement_paths`.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_stage tests.test_disk_decision_trace tests.test_disk_temp_budget tests.test_disk_trim tests.test_disk_temp tests.test_disk_caches tests.test_disk_report $(mods 'temporary\(|caches\(|report\(|_temp_')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Every stage now runs on one clock, the request dict's underscore keys are gone, and the temporary bridge with them."

### Task 36: A cut-off stage is partial

**Files:**
- Modify: `drover/disk_cleanup.py`, `drover/disk_runtime.py`, `drover/disk_cleanup_notices.py`, `tests/test_disk_stage.py`, `tests/test_disk_notices.py`, `tests/disk_decision_trace.json`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`

**Interfaces:**
- Consumes: `StageAnswer.partial`, `complete`.
- Produces: error row `stage_partial`; a run-level mark in `result`, read by `disk_cleanup_notices`, set at both `_serve_stage` and disk_cleanup.py:278-284.

- [ ] **Step 1: Failing tests.**
  ```python
  # tests/test_disk_stage.py
  def test_partial_stage_2_3_and_6_add_stage_partial_and_stamp_nothing(self): ...     # dry run, line 318 unchanged
  def test_applied_partial_stage_adds_no_stage_partial(self): ...
  def test_incomplete_stage_gets_todays_worker_error_row(self): ...
  def test_an_earlier_complete_reports_stamp_stands(self): ...
  def test_partial_stage_6_blocks_remove_approved_and_the_gates(self): ...            # disk.py:481, disk_schedule.py:34 see one stamp
  # tests/test_disk_notices.py
  def test_partial_stage_6_leaves_a_budget_episode_alone_at_both_call_sites(self): ...
  def test_the_mark_survives_retry_notices(self): ...
  def test_missing_usage_is_unknown_not_zero(self): ...                               # O7
  ```
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_disk_stage tests.test_disk_notices`. Must fail: `report_scope` is stamped, and the episode closes.
- [ ] **Step 3: Implement** per section 6.1. Check every reader of `budget_usage`, and list them in the slice report; at 92ce946 the judgement at disk_cleanup_notices.py:67 is the only one.
- [ ] **Step 4: Re-record and compare.** As Task 10 Step 4. Over the whole slice, allowed: stages stopping sooner; `can_mutate()` keeping a worktree; new deadline rows; `stage_partial`. Each is towards keeping or a report row. If stages 4 and 5 no longer stamp as today, the slice stops (ruling 7).
- [ ] **Step 5: Green.**
  ```bash
  M="tests.test_disk_stage tests.test_disk_notices tests.test_disk_decision_trace tests.test_disk_cleanup tests.test_disk_fresh_proof tests.test_disk_hub tests.test_placement"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 6: Slice exit.** All stages have one clock and keep their rows; `disk_evidence` holds inventory, proof, diagnostics, `Listings`; `can_mutate()` guards `planned`; symbols of row 5 removed; R6 fell; changed rows in the slice report; trace check clean. Commit: "A stage cut off at its deadline is now partial, so it stamps nothing and leaves budget judgement alone."

## Slice 6a: `disk_evidence` in full

Slices 6a, 6b and 6c move code. Each commit leaves `tests/disk_decision_trace.json` byte-identical. A test that patched a moved name moves its patch, and no test is rewritten for a move unless it says so.

### Task 37: `disk_inventory` takes the path facts

**Files:**
- Modify: `drover/disk_inventory.py`, `drover/disk_paths.py`, `drover/disk_temp.py`, `drover/disk_worktrees.py`, `drover/disk_registrations.py`, `tests/test_disk_evidence.py`, `tests/test_seams.py`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`, tests patching these names

**Interfaces:**
- Produces: `disk_inventory.worktree_roots`, `safe_path`, `repository_root`, `repository_config`, moved unchanged. `repository_config` keeps a fixed `NORMAL`, and any failure answers True (3M1).

- [ ] **Step 1: Red.** Add `drover.disk_paths.worktree_roots`, `.safe_path`, `.repository_root`, `.repository_config` to `qualified_names`. Add the `disk_inventory` direction rule to `tests/test_seams.py`, and:
  ```python
  # tests/test_disk_evidence.py
  def test_path_facts_live_in_disk_inventory(self):
      for name in ('repository_config', 'repository_root', 'safe_path', 'worktree_roots'):
          self.assertEqual(getattr(disk_inventory, name).__module__, 'drover.disk_inventory')
  def test_repository_config_answers_true_on_any_git_failure(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_seams tests.test_disk_evidence`. Must name the four symbols, and the test must fail on the old home.
- [ ] **Step 3: Move.** `disk_inventory.py:9` and :70 lose their `disk_paths` imports; `disk_paths`, `disk_temp`, `disk_worktrees`, `disk_registrations` import from `disk_inventory`. It ends near 240 lines. R6 falls by one.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_evidence tests.test_disk_decision_trace $(mods 'worktree_roots|safe_path|repository_root|repository_config')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "The path facts now live in disk_inventory, so disk_evidence and disk_authority can import them with no cycle."

### Task 38: Evidence reads

**Files:**
- Modify: `drover/disk_evidence.py`, `drover/disk_worktrees.py`, `drover/disk_paths.py`, `drover/disk_registrations.py`, `drover/disk_cleanup.py`, `drover/disk_cleanup_decisions.py`, `tests/disk_memory.py`, `tests/disk_fixture.py`, `tests/test_disk_evidence.py`, `tests/architecture_removed_symbols.json`, `tests/seam_exceptions.json`

**Interfaces:**
- Produces, as section 7.1: `StageEvidence.measure`, `status`, `head`, `gitdir`, `merged`, `tag`, `tracked`, `declarations`, `task_facts`, `fresh_snapshot`; module functions `identity`, `registration_identity`, `outputs_at`, `snapshot(machine, path, *, evidence)`. `disk_cleanup.scope_key` and `evaluate` import from here at load. `MemoryCleanupEvidence` is the second adapter.

- [ ] **Step 1: Red.** Add `drover.disk_worktrees.identity`, `.snapshot`, `.registration_identity` to `qualified_names`. Add to `tests/test_disk_evidence.py`:
  ```python
  def test_read_steps_take_patience_not_60_seconds(self):      # clock near the line: fake sees the clamped timeout on status, head, gitdir, tracked
  def test_a_failed_read_raises_and_the_row_is_kept(self): ...
  def test_only_report_only_runs_reuse_the_inventory(self): ...
  def test_fresh_snapshot_is_none_past_900_seconds(self): ...
  def test_needs_idle_gate_route_reads_fresh_snapshot(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_disk_evidence`. Must name the three symbols, and the patience test must show 60.
- [ ] **Step 3: Move** `head_at`, `gitdir_at`, `tracked_at`, `status`, `outputs_at`, `facts`, `task_facts`, `merged`, `fresh_snapshot`, `identity`, `snapshot`, `registration_identity`, adding `clock.patience(NORMAL)` on each read. Callers use `run.evidence`. `snapshot` keeps its own new `placement.cleanup_processes` call until Task 39. `evaluate`'s deferred import of `registration_identity`, disk_cleanup_decisions.py:130, goes: R6 falls here. Removal's mutating steps stay fixed until 6c.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_evidence tests.test_disk_decision_trace tests.test_disk_fresh_proof $(mods 'disk_worktrees|disk_paths|disk_memory|disk_fixture')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "One stage run's evidence reads now live in disk_evidence and take their time from the stage clock."

### Task 39: Fresh proofs have a type

**Files:**
- Modify: `drover/disk_evidence.py`, `drover/disk_worktrees.py`, `drover/disk_paths.py`, `tests/test_disk_evidence.py`, `tests/test_seams.py`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-6a.md`

**Interfaces:**
- Produces, as section 7.1: `FreshRegistrations(refused, overlaps)`, `FreshProcesses(proof)`, each refusing construction without this module's private token; `StageEvidence.fresh_processes()`; `StageEvidence.fresh_registrations(repo, target, own, *, pointer) -> FreshRegistrations`.

- [ ] **Step 1: Red.** Add `drover.disk_worktrees.overlaps_worktrees` to `qualified_names`; change R9's rule so `disk_evidence` is the only caller. Add:
  ```python
  def test_constructors_refuse_without_the_token(self):
      with self.assertRaises(TypeError): FreshRegistrations((), False)
  def test_fresh_registrations_reads_every_repo_once_and_one_tip_for_a_branch(self): ...
  def test_overlaps_comes_from_proof_reach_as_overlaps_worktrees_did(self): ...
  def test_a_repo_whose_listing_failed_raises_in_any_repo(self): ...
  def test_fresh_processes_is_always_a_new_snapshot(self): ...
  def test_snapshot_takes_fresh_processes_never_processes(self): ...
  def test_proof_timeout_is_patience(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_seams tests.test_disk_evidence`. Must name `overlaps_worktrees` and show `disk_worktrees` calling `prove`.
- [ ] **Step 3: Implement.** Removal callers take `fresh_registrations`, and `snapshot` takes `evidence.fresh_processes()`. `Listings` construction reads as section 7.1.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_evidence tests.test_disk_decision_trace tests.test_disk_fresh_proof tests.test_disk_removal tests.test_disk_outputs tests.test_git_worktrees"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Slice exit.** `disk_evidence` whole, R9's caller is `disk_evidence` alone, `disk_inventory` imports no `disk_paths`, row 6a's symbols removed, trace check prints nothing. Commit, with the slice report: "Removal now needs a fresh proof object that only disk_evidence can make."

## Slice 6b: `disk_authority`

### Task 40: `decide` and `CleanupState` move

**Files:**
- Create: `drover/disk_authority.py`, `tests/test_disk_authority.py`
- Modify: `drover/disk_cleanup_decisions.py`, `drover/disk_state.py`, `drover/disk_cleanup.py`, `tests/test_seams.py`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`, tests naming these

**Interfaces:**
- Produces: `Ruling`; `disk_authority.decide`, `decide_and_store`, moved verbatim; `disk_state.CleanupState`. `disk_cleanup_decisions` keeps `CleanupRuntime` and `evaluate`, importing at load.

- [ ] **Step 1: Red.** Add `drover.disk_cleanup_decisions.decide` and `.CleanupState` to `qualified_names`. Add the authority rule to `tests/test_seams.py`, no `process`, no `git_client`, and:
  ```python
  # tests/test_disk_authority.py
  def test_decide_is_pure_and_changes_neither_argument(self): ...
  def test_authority_writes_only_quarantine_state(self): ...      # AST: no write, unlink, rmtree in disk_authority
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_seams tests.test_disk_authority`. Must name both symbols, with `ModuleNotFoundError` for the new home.
- [ ] **Step 3: Move** them, byte for byte in `decide`'s body. `disk_cleanup_decisions` falls to about 30 lines.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_authority tests.test_disk_decision_trace tests.test_disk_quarantine $(mods 'decide_and_store|CleanupState|disk_cleanup_decisions')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Selection and grace now live in disk_authority, and the state adapter in disk_state, with no behaviour changed."

### Task 41: Disposable rules

**Files:**
- Modify: `drover/disk_authority.py`, `drover/disk_paths.py`, `drover/disk_preflight.py`, `drover/disk_budget.py`, `drover/disk_worktrees.py`, `drover/disk_temp.py`, `tests/test_disk_authority.py`, `tests/test_disk_fix8.py`, `tests/test_disk_wsl_outputs.py`, `tests/test_disk_elevated.py`, `tests/test_disk_fix3.py`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`

**Interfaces:**
- Produces, as section 7.2: `rule_disposable`, `rule_disposable_fresh(run, machine, registration, processes: FreshProcesses, *, touched=())`, `rule_unit`, `rule_unit_fresh(run, unit, proof: FreshRegistrations)`, `DisposableUnit`, `protected_path`, `repository_tree`, `live_tree`, `disposable_match`, `ignored_outside`. `Ruling.facts` carries `(path, head, tag, declarations)`.

- [ ] **Step 1: Red.** Add `drover.disk_paths.output_evidence` to `qualified_names`. A rule stops raising (section 7.2), so these four **are rewritten** against `Ruling`: test_disk_fix8.py:267, test_disk_wsl_outputs.py:36, test_disk_elevated.py:95, test_disk_fix3.py:23. Add to `tests/test_disk_authority.py`:
  ```python
  def test_a_rule_returns_a_ruling_and_never_raises_to_mean_no(self):
      # one subTest per output_evidence rejection, through a StageEvidence fake
  def test_idle_waiver_moved_verbatim(self): ...                 # hold, critical; a budget never
  def test_fresh_rule_refuses_a_first_pass_proof(self): ...      # a plain ProcessProof: TypeError
  def test_facts_carry_what_disposable_rows_unpack(self): ...
  def test_reason_code_and_error_text_are_stable(self): ...
  def test_rule_unit_fresh_reads_overlaps_and_refused(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_disk_authority`. Must name `output_evidence`, with `AttributeError` for `rule_disposable`.
- [ ] **Step 3: Move.** `output_evidence`'s `fresh=True` branch becomes `rule_disposable_fresh`. `ignored_outside` leaves disk_worktrees.py:297, and `disk_worktrees` and `disk_temp` import these names from `disk_authority`. `check_cache_folders` imports `repository_tree` inside the function: an R6 end row. `disk_paths` falls towards 215 lines.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_authority tests.test_disk_decision_trace tests.test_disk_fresh_proof $(mods 'output_evidence|disposable_match|protected_path|live_tree|repository_tree|ignored_outside') tests.test_config"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Every disposable-path rule now lives in disk_authority and answers with a ruling instead of raising."

### Task 42: Worktree, quarantine, temp and cache rules

**Files:**
- Modify: `drover/disk_authority.py`, `drover/disk_worktrees.py`, `drover/disk_temp.py`, `drover/disk_paths.py`, `tests/test_disk_authority.py`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-6b.md`

**Interfaces:**
- Produces, as section 7.2: `inspect(run, machine, registration, *, processes: ProcessProof)`, `quarantine_observation`, `quarantine(run, machine, row, *, store=None)`, `unlisted_live`, `needs_idle_gate`, `rule_temp_entry`, `rule_cache_file`, `CLOSED_IDLE_S`.

- [ ] **Step 1: Red.** Add `drover.disk_worktrees.inspect`, `.quarantine` to `qualified_names`. Add:
  ```python
  def test_inspect_requires_processes(self): ...                         # TypeError without it
  def test_missing_row_keeps_head_from_the_listing_and_needs_no_tip(self): ...
  def test_rule_temp_entry_holds_only_the_static_checks(self): ...       # scan, cache, budget stay in disk_temp
  def test_rule_cache_file_age_and_level(self): ...
  def test_needs_idle_gate_goes_only_after_three_days_quiet(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_disk_authority`. Must name both symbols.
- [ ] **Step 3: Move.** Stages ask; they stop deciding. Note the `disk_temp` split: only `temp_facts`' static checks move. `WorktreeEvidence` stays in `disk_worktrees`. R6 falls.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_authority tests.test_disk_decision_trace tests.test_disk_temp tests.test_disk_caches tests.test_disk_yanked tests.test_disk_quarantine $(mods 'quarantine|inspect\(|needs_idle_gate|unlisted_live|temp_facts|folder_cache')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Slice exit.** Every rule of section 7.2's map is where it says; authority import rule live; row 6b's symbols removed; trace check prints nothing. Commit, with the slice report: "Every rule that says whether a path may go now lives in disk_authority, and the paths-worktrees cycle is gone."

## Slice 6c: `disk_removal`

### Task 43: `remove_worktree`

**Files:**
- Create: `drover/disk_removal.py`
- Modify: `drover/disk_worktrees.py`, `tests/test_disk_removal.py`, `tests/test_disk_fresh_proof.py`, `tests/test_seams.py`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-6c.md`

**Interfaces:**
- Produces, as section 7.3: `remove_worktree(run, selected, *, store) -> dict`; `unlock_at`, `lock_at`, `discard_at`, `rescue_at` as private steps, each under `clock.mutation_cap()`.

- [ ] **Step 1: Red.** Add `drover.disk_worktrees.remove` to `qualified_names`. Add to `tests/test_disk_fresh_proof.py`, default tier, faked at `process`:
  ```python
  def test_timed_out_worktree_remove_ends_failed_with_the_marker_restored(self): ...      # ruling 4
  def test_timed_out_lock_at_on_the_restore_path_still_journals_failed_with_both_errors(self): ...
  def test_every_step_between_planned_and_completed_takes_mutation_cap(self):
      self.assertEqual({options['timeout'] for step in mutating}, {60.0})      # rev-parse of the rescue ref included
  def test_tiny_mutation_cap_still_ends_in_a_failed_row(self): ...                         # Review Focus 4: worker_timeout_s=11
  def test_failed_planned_append_stops_before_any_mutation(self): ...
  def test_journal_before_unlock_or_delete(self): ...
  def test_one_fresh_proof_per_removal_and_one_new_process_proof(self): ...
  def test_ordering_is_section_7_3(self): ...      # row, inspect, fresh_registrations, decide, nesting, ignored, can_mutate, planned
  def test_unlisted_target_has_no_row_and_no_tip(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_disk_fresh_proof`. Must name `disk_worktrees.remove`, and the lock-restore test must show no `failed` row, the bug at disk_worktrees.py:384-386.
- [ ] **Step 3: Implement** by section 7.3's five steps. The lock restore catches its own failure, so `failed` is always written, with both errors. `restore_argv` comes from `git_client.argv`, and the journal format is unchanged. A `git_client.mutate` that returns not ok raises to the existing handling. Removal's imports stay `inspect`, `quarantine`, `unlisted_live` from `disk_authority`; `identity`, `snapshot` from `disk_evidence`.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_fresh_proof tests.test_disk_decision_trace tests.test_disk_removal tests.test_disk_quarantine tests.test_disk_worktrees $(mods 'disk_worktrees\.remove|remove\(request')"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
  The trace must not change. The two restore-path tests add assertions to rows that today's trace does not reach. If one does reach it, report BLOCKED.
- [ ] **Step 5: Commit**: "Worktree removal now lives in disk_removal, with every mutating step capped and a failed restore still journalled."

### Task 44: Disposable, temp and cache removal

**Files:**
- Modify: `drover/disk_removal.py`, `drover/disk_paths.py`, `drover/disk_temp.py`, `drover/disk_budget.py`, `tests/test_disk_removal.py`, `tests/seam_exceptions.json`, `tests/architecture_removed_symbols.json`

**Interfaces:**
- Produces: `remove_disposable(run, unit: DisposableUnit) -> None`, `remove_temp_entry(run, row, path) -> None`, `remove_cache_file(run, row, path, info) -> None`.

- [ ] **Step 1: Red.** Add `drover.disk_paths.remove_output` to `qualified_names`. Delete R4's rows that these moves make false. Add to `tests/test_disk_removal.py`, default tier:
  ```python
  def test_disposable_ordering_is_section_7_3(self): ...
  def test_bytes_count_as_each_path_goes_on_a_removal_cut_short(self): ...
  def test_a_disposable_with_no_dot_git_is_refused(self): ...       # `pointer` True
  def test_stage_abandoned_never_raised_inside_remove(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_architecture tests.test_seams tests.test_disk_removal`. Must name `remove_output` and the R4 modules.
- [ ] **Step 3: Move** `remove_output`, `prune_emptied`, and the delete lines in `disk_temp` and `folder_cache`. `DisposableUnit` stays in `disk_authority`, with the `partial` at disk_paths.py:409 replaced by it, field for field: `purge` holds the unit and calls `remove_disposable(run, unit)` where disk_budget.py:90 calls `removal`.
- [ ] **Step 4: Green.**
  ```bash
  M="tests.test_disk_removal tests.test_disk_decision_trace tests.test_disk_outputs tests.test_disk_wsl_outputs tests.test_disk_temp tests.test_disk_temp_budget tests.test_disk_caches tests.test_disk_budget tests.test_disk_budget_folders"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 5: Commit**: "Every deletion by path in the disk family now happens in disk_removal."

### Task 45: Guards complete; conversions

**Files:**
- Modify: `drover/disk.py`, `drover/disk_cleanup.py`, `drover/disk_paths.py`, `drover/disk_registrations.py`, `drover/disk_state.py`, `drover/disk_worktrees.py`, `drover/pane_fault.py`, `drover/placement_proof.py`, `drover/wizard.py`, `drover/worktree.py`, `tests/test_seams.py`, `tests/seam_exceptions.json`, `tests/__init__.py`, `tests/test_guards.py`, disk test modules being converted, `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-6c.md`

**Interfaces:**
- Produces: R4 complete, R6 at 3, R7 live, R1 at 9.

- [ ] **Step 1: Red.** Add to `tests/test_seams.py`:
  ```python
  def test_r7_no_first_pass_call_in_disk_removal(self): ...         # planted source and the real tree
  def test_r7_fresh_constructors_appear_only_in_disk_evidence(self): ...
  def test_r6_count_is_exactly_the_three_stated_rows(self): ...     # check_cache_folders, run_detached, cleanup_runtime
  def test_r1_is_exactly_the_nine_stated_modules(self): ...
  ```
- [ ] **Step 2: Run red.** `DROVER_TESTS=integration $PY -m unittest tests.test_seams`. Must print each stale row or count.
- [ ] **Step 3: Remove** the last tuple-only `subprocess` imports, with their R1 rows, in the production files above, `disk_cleanup`'s module-level one included: its `locked_command` import stays. Remove stale deferred imports. Check section 7.4's line budgets, and record actuals in the slice report. Public names in the cluster: count against "about 15".
- [ ] **Step 3a: Git fake shapes** (spec Appendix C, slice 4). Inherited from Task 28: process-level git fakes are `FakeCommands.patch(process)` and `tests.fakeci.git_answers`, with `git_spy` on the same parser. Convert or rule `fakeci.answer_spawns`/`fakeci.answering` and the direct `process.subprocess.run` fakes; add no new shape.
- [ ] **Step 4: Convert.** For `test_disk_fix7`, `test_disk_fix8`, `test_disk_fixes`, `test_disk_report`, `test_disk_boundaries`, `test_disk_worktrees`, and any test in Task 1's candidates still spawning for setup: use `StageEvidence` fakes or `MemoryCleanupEvidence`, in place, one commit per module, lowering the constant. A real-git contract stays, and is named.
- [ ] **Step 5: Green.**
  ```bash
  M="tests.test_disk_decision_trace tests.test_disk_fresh_proof tests.test_disk_authority tests.test_disk_evidence tests.test_disk_stage tests.test_disk_fix7 tests.test_disk_fix8 tests.test_disk_fixes tests.test_disk_report tests.test_disk_boundaries tests.test_disk_worktrees"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers tests.test_seams $M tests.test_guards
  ```
- [ ] **Step 6: Slice exit.** Every deletion in `disk_removal`; each git step under `mutation_cap()`; R4, R6, R7, R1 at their end states; row 6c's symbols removed; trace check prints nothing. Commit: "The seam guards reach their end state, so a first-pass value can no longer authorise a deletion."

### Task 46: Checkpoint after slice 6c

**Files:**
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-checkpoint-6c.md`
- Modify: `docs/superpowers/reviews/2026-11-02-rearchitecture-test-times.md`

Runs the Checkpoint protocol with `LAST_CP` as Task 29's report commit, covering slices 5, 6a, 6b, 6c.

- [ ] **Step 1: Run the protocol.** Targets: the row after slice 6c.
- [ ] **Step 2: Reviewer focus.**
  - Completeness: sections 6, 7, 9 row by row; every invariant bullet of section 6.1 with its test; ruling 4's restore path; ruling 7's pins; Review Focus 4; every row of section 7.2's rule map; both orderings of section 7.3.
  - Scope: section 10 rows 5 to 6c; `git log -p` on `tests/disk_decision_trace.json` shows re-records only in Tasks 34 to 36 of slice 5 and none in 6; `_bridge` is gone; `decide` is byte-identical to 92ce946, by `git show` and diff; no rule changed meaning; `disk_side` is still the only `LEAF_ALLOWED` name `placement_worker` imports.
  - Efficiency hot paths: `add` and its cap; `checkpoint`'s flush per removal unit; `prove` per removal against section 2.5; `patience`; no stage holding more rows than `compact` keeps; a stage's listing count. Also duplicates between `test_disk_authority`, `test_disk_fix*` and the trace.
- [ ] **Step 3: Fix findings**, by the protocol, then record `Task 46: complete`.
- [ ] **Step 4: Commit** both reports: "The slice 6c checkpoint records its test times and its three reviews."

## Slice 7: `run_output`

### Task 47: `RunOutput`

**Files:**
- Create: `drover/run_output.py`, `tests/test_run_output.py`, `docs/superpowers/reviews/2026-11-02-rearchitecture-slice-7.md`
- Modify: `drover/dispatcher_launch.py`, `drover/dispatcher_finish.py`, `drover/outside_land.py`, `tests/test_scope_base.py`, `tests/test_seams.py`, `GLOSSARY.md`

**Interfaces:**
- Produces, as section 8: `RunOutput(base, scope_files, worktree)` with `to_event`, `from_event(value) -> RunOutput | None`, `owns(worktree) -> bool`. `LaunchSteps._output_snapshot` returns `RunOutput`; `_spawn` stores `to_event()`. `RecoverySteps._adopt` is unchanged. The 22 mixin rows do not change.

- [ ] **Step 1: Failing tests**, `tests/test_run_output.py`, default tier:
  ```python
  def test_from_event_table(self):
      for stored, want in ((THREE_KEY_DICT, RunOutput('b', ('f',), 'w')), ({'base': 'b'}, RunOutput('b', (), None)),
                           ({'base': 'b', 'extra': 1}, RunOutput('b', (), None)), ({'base': ''}, None), ({}, None), (None, None)):
          self.assertEqual(RunOutput.from_event(stored), want)
  def test_to_event_writes_the_same_three_keys(self): ...
  def test_owns_is_the_check_at_dispatcher_finish_246(self): ...
  def test_outside_land_fixture_one_key_dict_decodes(self): ...
  ```
  Change `tests/test_scope_base.py:93` from `["base"]` to `.base`. Add `test_run_output_imports_no_drover_module` to `tests/test_seams.py`.
- [ ] **Step 2: Red.** `DROVER_TESTS=integration $PY -m unittest tests.test_run_output` fails with `ModuleNotFoundError` for `drover.run_output`.
- [ ] **Step 3: Implement.** Replace the dict at dispatcher_launch.py:43, `spec['output_snapshot']` reads at dispatcher_finish.py:244-250, and `.get('base')` at outside_land.py:151. The docstring keeps the base rule. `outside_land` is near its size, and the file does not grow.
- [ ] **Step 4: Glossary.** Add section 15's terms to `GLOSSARY.md`, in its format.
- [ ] **Step 5: Green.**
  ```bash
  M="tests.test_run_output tests.test_scope_base tests.test_dispatcher_launch tests.test_dispatcher_recovery tests.test_outside_land tests.test_lifecycle_characterization"
  DROVER_TESTS=integration $PY -m unittest $M $GUARDS
  env -u DROVER_TESTS $PY -m tests.tiers $(changed) tests.test_guards
  ```
- [ ] **Step 6: Slice exit.** Trace check prints nothing. Commit, with the slice report: "A run's output is now a record with a codec that reads every stored shape, and the glossary names the new concepts."

## End

### Task 48: Final checkpoint

**Files:**
- Create: `docs/superpowers/reviews/2026-11-02-rearchitecture-checkpoint-final.md`
- Modify: `docs/superpowers/reviews/2026-11-02-rearchitecture-test-times.md`

Runs the Checkpoint protocol over the whole branch: `LAST_CP` is 92ce946 for all three reviewers. This is also the whole-branch final review `subagent-driven-development` asks for.

- [ ] **Step 1: Run the protocol.** Targets: the End row. Report the integration and `IMPURE` numbers against 209 and 65, and total default-tier time against the baseline.
- [ ] **Step 2: Reviewer focus.**
  - Completeness: section 17's table: for each defect family, the test that fails if it returns. Every Review Focus line. Section 8.
  - Scope: section 1's scope; every section 10 landing row; `tests/architecture_removed_symbols.json` holds every listed name; `git diff --stat 92ce946 -- tests/architecture_import_allowlist.json` shows no added row; no slice 8 code; `tests/seam_exceptions.json` is exactly section 13's end column; `git log` shows no merge, and no push happened.
  - Efficiency: the checkpoint targets end to end; section 2.5's cost against the slice 1 timing; the remaining real-process tests, each with a reason.
- [ ] **Step 3: Fix findings**, by the protocol, then record `Task 48: complete`.
- [ ] **Step 4: Commit** both reports: "The final checkpoint records the branch's test times and its three reviews." The branch stops here: nothing is merged, pushed or deployed by a subagent.

### Task 49: Owner checks

No agent runs this. The controller hands the owner this list, in this order, unchanged (section 19; ruling 11 sets the order). If a later check fails, revert the merge and redeploy the previous commit: no stored format changed.

| # | Check | Command or action | Pass means |
|---|---|---|---|
| 1 | Base commits | Finish review of the four commits up to 92ce946 on `fix/cleanup-stage4` | They land unchanged. If any changes, stop and tell the session lead: the trace is re-recorded (ruling 5) |
| 2 | O1 | Decide the import allowlist: a removal plan, or a ban on new bindings | The choice is recorded. Neither choice adds rows |
| 3 | Merge and push | Merge `arch/rearchitecture` into `main`, with the base commits, then `git push` | The gate passes on the merged commit, and a gate receipt exists for it |
| 4 | Deploy | `python scripts/deploy_hub.py --deploy <dir> --commit <sha> --receipt <receipt.json> --restart` | The hub restarts on `<sha>`, and its UI answers on port 8765 |
| 5 | Live dry run from `<dir>`, at normal WSL load. Copy `last-report.json` aside first, for check 6. It writes `report_scope` and `last-report.json` to the real state, so a pass unlocks apply from this code | `python -m drover disk-cleanup --dry-run --json` | It finishes with no `worker_timeout`, no `worker_failed` and no listing timeout. `errors` is empty or only `excluded_repo`, and no `stage_partial`. `report_scope` is stamped. A dry run never reads admin files, so this proves listing, not the fresh proof. A `stage_partial` is safe but means apply stays locked: see check 9 |
| 6 | Dry report against the last one | Compare the new `last-report.json` with the copy, by `reason_code` | No row has moved to `candidate` without a reason listed in a slice report |
| 7 | Live apply from `<dir>`. `daily` runs stages 1 to 4 at any drive level. Without `--mode`, the default stops a drive at or above its warn level, so it would remove nothing on a healthy machine, and only it reaches stage 5 | `python -m drover disk-cleanup --mode daily --apply --json`; when a drive is below warn, also `python -m drover disk-cleanup --apply --json` | Every journal `planned` has a `completed` or a `failed`. No `failed` left unexplained. No row removed that check 6 did not show. No unlock left behind on a kept worktree. Every `registration_unknown` row is read and understood: an unknown with no readable path is the owner item of ruling 9. A run with no `removed` rows passes nothing: it proves no removal |
| 8 | O4 | From check 7, divide the stage's elapsed time by its removal units, and compare with the builders' read-only timing of the proof's read | The read is a quarter of a removal or less: slice 8 stays unbuilt. More: decide slice 8, and say whether it sits within ADR-0007. If no removal ran, O4 stays open until one does |
| 9 | O2, and what a stage deadline costs | Read checks 5 and 7 for a repo error or `stage_partial` that kept apply locked | No action if neither happened. If one did, rule on O2, or raise `worker_timeout_s` |
| 10 | First scheduled run | Wait for the next daily cleanup, then read `last-report.json` | It completed, with no worker error. The scheduled path works under the deployed hub |

Listed for the owner, accepted by ruling 14: a repo breaking after its listing in `config`, an extension or `refs` goes unseen by other repos' removals until the next stage.

- [ ] **Step 1: Hand over.** Give the owner this table, the checkpoint reports and the test-times report. Record `Task 49: complete` when handed over; the checks themselves are the owner's.
