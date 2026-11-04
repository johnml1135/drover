# Slice 6c: `disk_removal` whole, build roots, and the fixes since slice 5

`SLICE_BASE`: b0524d5 (slice 6b shipped at cb6a23b, then the fixes and the build-roots feature below, shipped at b0524d5). Commits: Tasks 43 (cf428fc), 44 (474f5b8, 87746c8) and 45 (cd2ef78..e6db4c4), plus fe4662a from the exit review. The exit review covered 16a3623..e6db4c4.

## What the slice changed

- Task 43: `remove_worktree(run, selected, *, store)` and its steps (`unlock_at`, `lock_at`, `discard_at`, `rescue_at`) moved into the new `disk_removal` (153 lines then). Every step, rescue's verifying `rev-parse` included, takes `mutation_cap()`. `lock_at` raises `GitFailed` when `mutate` is not ok, so a restore that fails twice reports both errors and the `failed` row is always journalled. `completed` and `failed` appends retry within the cap; a failed `planned` append is not retried and writes no lone `failed` row.
- Task 44: `remove_disposable(run, unit)`, `remove_temp_entry`, `remove_cache_file` and `prune_emptied` moved into `disk_removal`, with `REMOVAL_DEADLINE`. Each takes the `can_mutate` gate after its fresh proofs. `_close` became the public `close_journal(store, record, phase, clock, row=None)`, with a retry window of `min(clock.mutation_cap(), clock.left())`, and `reregister`'s closing appends use it. `disposable_rows` yields `(row, DisposableUnit)` pairs. Four R4 rows went. `remove_disposable` catches `(OSError, ValueError, StopIteration)` (ruling on `repository_config`: no `SubprocessError`).
- Task 45: R1 (9 modules, `locked_command` alone kept in `disk_cleanup`), R6 (3 rows) and R7 (a first-pass call in `disk_removal`, or `FreshRegistrations(` and `FreshProcesses(` outside `disk_evidence`) reached their end state. `seam_exceptions.json` went from 37 to 24 rows. `subprocess` imports and `SubprocessError` arms went from ten modules. `_token` is an `InitVar` and is never stored. Spec 7.1 records `prove=None` and 7.2 records `rule_cache_file`'s `entry=`. The git fake shapes are ruled in Appendix C. `IMPURE_CEILING` went from 59 to 56; `INTEGRATION_CEILING` is unchanged (205).
- Build roots (efd517c, 4147beb): `[[disk_cleanup.build_roots]]` makes each immediate subfolder idle for `max_idle_hours` (default 2) removable regenerable output, in stage 2.
  - Process proof: the budget treatment, through `processes(regenerable=True)` and `fresh_processes(regenerable=True)`.
  - Rules: `rule_build_unit` and `rule_build_unit_fresh` in `disk_authority`; `disk_temp.build_roots` runs after the strict proof.
  - Temp overlap: a build root wins and is handled once.
  - Preflight: refuses drive and filesystem roots, home and its ancestors, system folders, repositories, and overlap with state, repos and worktree roots.
  - Device: a unit's walk compares `st_dev` with the root's.
  - New trace scenes `build_root.dry` and `build_root.applied`; `build_root_recent` joined ON_THE_CLOCK.
- Fixes since slice 5:
  - ec8c1ff: the Windows proof re-checks unreadable processes and drops one only when it is gone or its pid was reused. `process_ignore` names for unreadable images stay budget and pane-close only. Opus review: approved, no Critical or Important.
  - 4c3d2b7: `state.lock` waits up to 5 s; `disk-cleanup.lock` gives 1 s for a missing `owner.json` and fails fast on a live holder.
  - 1bcf2e9: stage 4's suggestions read each repository's history once per report (`History`), and past the line none is made. Opus review: approved; the Important was that it is unproven end to end until a post-deploy run times stage 4.
  - 6e5319a: the report-only suggestion is skipped for lost folders off the stage's drive. Accepted on the controller's read of the diff.
  - fe4662a: stage 4's stray and lost-folder loops check `late()` before each folder and checkpoint each row as it settles (the exit review's Completeness Important).

The decision trace changed three scenes, all towards keeping (Task 44), and gained two new scenes (build roots); see Exit checks.

## Review outcomes

- Task 43: spec ok, approved, 0 Critical, 0 Important, 5 Minor.
- Task 44: spec ok, approved, 0 Critical, 0 Important, 4 Minor.
- Build roots (efd517c): Quality changes needed, 0 Critical, 1 Important, 4 Minor. The Important (the drive-root preflight gap, a mount check against the root's `st_dev`) was fixed in 4147beb; the re-review found both items addressed.
- Task 45: reviewed in the exit review; spec ok, approved, Minors only.
- Slice exit (`checkpoint-6c-review.md`, one Opus reviewer): 0 Critical, 3 Important, 10 Minor. Data-loss sweep found none.
  - I1 Completeness (stage 4 stray and lost loops not checkpointed or checked against `late()`): fixed in fe4662a.
  - I2 Scope (the branch did not carry slice 6's rulings): this batch (Appendix C, section 10, section 7.4 and this report).
  - I3 Efficiency (the stage-5 per-file unit, pre-existing): owner ruling, a separate task after this slice, with an Opus implementer and reviewer. Not in 6c because it moves the trace in the deletion direction.

## Exit checks

| Check | Command | Result |
|---|---|---|
| Trace changes are the ruled ones | `git log -p tests/disk_decision_trace.json` over 16a3623..e6db4c4 | slice 5 (6bedb29, 1a30f79) as allowed; efd517c adds `build_root.dry` and `build_root.applied` and removes no line; 87746c8 changes exactly `output_deadline_between_units.applied`, `output_deadline_between_worktrees.applied` and `budget_deadline_mid_purge.applied`, each `removed` to keep `removal_deadline` |
| Trace flake check | `trace_flake.py` before and after Task 45 | `SAME []` |
| `decide` and `decide_and_store` unmoved in meaning | AST extract diffed against 92ce946 | byte-identical |
| No first-pass value authorises a deletion | read of `remove_worktree`, `remove_disposable`, temp and cache stages | none; every path deletion is in `disk_removal` (R4 complete) |
| No removal past the line | `can_mutate()` in the four `remove_*` and `reregister` | all five; `can_mutate` includes `not late()` |
| Guards | `scripts/test.py` guards; default tier `tests.test_seams`, `test_guards`, `test_architecture` | 42 OK; OK (skipped=6) |
| Task 45 integration | `DROVER_TESTS=integration` over M1 and M2 | `Ran 236 tests` OK (skipped=5); `Ran 52 tests` OK (skipped=3) |
| Stage 4 fix | `scripts/test.py tests.test_disk_stage_removals tests.test_disk_registration_timeouts` | `Ran 35 tests`, 0 failed modules |
| Full suite at the ship commit | `scripts/test.py -j 4`, both tiers, at c8704e6 | default 4390 (28 skipped), 0 failed modules; integration 205 (14 skipped), 0 failed modules |
| First slice-exit run (72cdb4d) | same | 3 regressions: Task 45's `SubprocessError` removal broke `test_pane_faults` and `test_disk_placement` (restored, 401a449, R1 = 11); `test_pipeline_contract_landing`'s restart child imported `tests` and read the real H: below the 50 GB hold (latent since 4be8324; fixed c8704e6) |

## Line budgets and public names (spec 7.4, restated)

Actuals: `disk_worktrees` 126 (target 140), `disk_paths` 261 (215), `disk_budget` 103 (85), `disk_temp` 219 (100), `disk_registrations` 390 (280), `disk_authority` 467 (450), `disk_evidence` 319 (280), `disk_removal` 243 (280), `disk_stage` 359 (250), `disk_inventory` 237 (240), `disk_cleanup_decisions` 33 (30), `disk` 488, `disk_cleanup` 419 (spec 403).

- Public names used outside their file: about 70 (AST count; 66 by Task 45's regex), 21 in `disk_authority`, against "about 15". The target contradicted 6.1 and 7.1 to 7.3, which name about 40 cluster names. The stage modules export 1 to 4 each. 7.4 now says about 70, with no stage module above 4.
- `disk_cleanup` grew from 403 by slice 5 (8052564, 2447b3d), not by build roots.
- `disk_authority` is at 467 of 500: a split is planned (stage-2 rules to their own module). `disk_registrations`' report-only `History` and suggestion code is separable.

## Parked Minors (to the final review)

- Task 43: M2 a torn line on a retry (the manifest is unread); M3 `_StateLock.__exit__` `RuntimeError` or `ValueError` after `completed` (pre-existing); M5 this document (now written).
- Task 44: M1 a lost journal note when the holder cleanup also fails; M2 a partial temp `rmtree` counts 0 bytes and is marked protected (pre-existing); M4 `SubprocessError` is not caught (ruled).
- Build roots: `rmtree` is not bounded by the stage clock (same as temp); the `removal_deadline`, `drive_unproven` and root-is-a-junction branches have no test; the accept test does not use the literal `C:\cargo-targets`; a UNC share root is not refused.
- Exit review: `protect` reads and the `gc.worktreePruneExpire` write, `common_dir`, judge and `hash-object` use fixed 60 s timeouts, not `patience`, and the write has no `can_mutate`; `git worktree remove -f -f` at `disk_registrations.py:379` sits outside `disk_removal` and R4 cannot see it; `disk_removal` reaches `ownership` and `disk_inventory` with no R5 direction rule, and calls `ownership.read` directly; R7 matches only `ast.Call`, so an uncalled bound method such as `LazyProof(evidence.processes)` passes; `StageRun.add` deep-copies a row before it caps `dirty`, `ignored` and `excluded`; the purge holds each `DisposableUnit.measured`, more than `compact` keeps; a nit in `disk_inventory.py` (`wsl =repos.worktree_root(`).

## Carries

- `disk_evidence._out` is imported by `disk_removal` and `disk_registrations` and carries mutating steps: fix before the final review.
- The stage-5 per-file removal unit: its own task after this slice (Opus implementer and review; trace re-record in the deletion direction).
- `disk_authority` split (467/500) and `disk_registrations` suggestion module: plan now.
- `lost()` is not filtered by drive (ruled): motif's 16 folders cost about 12 min on both drives' judging. Stage 4's time on the real drives is unproven until the post-deploy cleanup run.
- Removing `SubprocessError` from the tuples holds only while `git_client` and `process` stay the only spawners.
- The `test_trace_matches_the_recording` load flake (126 s under load, once): if it fails again, `trace_flake.py` prints the scene.

## Mutant checks for the slice gate

To run once at slice end, each caught by a named test or trace scene:
- Task 44: the second clock checks in stale temp (`disk_temp.py:199`) and the folder cache (`disk_paths.py:86`); the `remove_disposable` `can_mutate` gate line.
- Task 45: an R7 violation (a `listed()` call in `disk_removal`; a `FreshProcesses(x, None)` outside `disk_evidence`); `_only_here`'s token check dropped; a `SubprocessError`-raising fake at the changed `except` sites; `disk_runtime.cleanup_runtime` late binding of `disk._free`.
- Build roots and fixes: the survivor tests W2, B4a, B7b, B8b, B8d and B10c (killed 6/6 in b0524d5); B8a (a root-level `late()` stop) is parked as near-equivalent.

## Test times

Taken under load (a live cleanup apply and Motif test runs on the machine), so they overstate: default tier wall 226.5 s (4390 tests), integration wall 274.9 s (205 tests); integration modules over 60 s: `test_disk_registrations`, `test_disk_yanked`. The slice 6b ship run was 145.9 s / 198.9 s on a quieter machine. Re-time on an idle machine before the final checkpoint.

## Mutant gate

52 mutants over b0524d5..82e7d58: 40 killed. Six equivalent (T44h, F8, F9, F17 POSIX-only, F19, F23; reasons in the workspace closeout). Six real gaps (T44e, T44f, T44g, S4e, F15, F21) got tests in 72cdb4d and are killed 6/6. T44f and T44g use a refusing clock subclass: a real `StageClock` cannot refuse `can_mutate` while not late.

The stage-5 folder-unit task (46b: ccf0048, 5dfe5b0, e9adef7, 82e7d58) ran in this slice after the exit review: its trace re-record moves towards deletion only where the same files and bytes now go as one folder (36 entries, each verified by an independent Opus review). The repository-evidence rule stays as it is (a lone `config` or one git marker keeps the folder); narrowing it to git's git-dir shape inside stage-5 folder walks is an owner option.
