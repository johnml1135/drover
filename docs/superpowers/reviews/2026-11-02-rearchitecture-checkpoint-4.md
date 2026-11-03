# Rearchitecture checkpoint after slice 4

Range 4d8039d..5638837: slice 4 (Tasks 22-28, `git_client`), then this checkpoint's fixes.

## Test runs

| Run | At | Result |
|---|---|---|
| Default tier, `-j 4` | e0c7b1e | 4146 tests, 28 skipped, 276 modules, 220.7 s, 0 failed |
| Integration tier, `-j 4` | e0c7b1e | 208 tests, 14 skipped, 231.8 s, 0 failed |
| Default tier, `-j 4` | 5638837 | 4165 tests, 28 skipped, 277 modules, 179.5 s, 0 failed |
| Integration tier, `-j 4` | 5638837 | 205 tests, 14 skipped, 185.9 s, 0 failed |

The efficiency review could not pin the 220.7 s on slice 4: the converted modules fell and the slowest tests sit in untouched modules, so machine load is the likely cause, and the rerun took 179.5 s. `test_landing_repository` fell from 60.1 s to 28.5 s; `test_disk_registrations` (107.2 s) is untouched by slice 4.

Census at e0c7b1e: the six modules Task 28 converted went from 568 to 47 real spawns and from 28.0 s to 9.1 s; `test_origin_sync` from 234 to 0. `IMPURE_CEILING` 63 to 59, `INTEGRATION_CEILING` 208 to 205.

## Reviews

All three failed first; one fix batch (15 commits, e51296c..5638837, tests and docs only) resolved every Important finding, and the scoped re-review passed with all 24 named mutants killed.

| Reviewer | Important | Outcome |
|---|---|---|
| Completeness | 7 missing pins: ControlledGit handed to origin; worktree_metadata's WSL IO on a run that does not exit; repos.load's RepoError; disk_worktrees.merged's unknown arms; section 5.1 timeouts; repository_config's `or parsed.err`; disk_registrations.candidate's unknown | A test for each, red on its mutant first |
| Scope | Slice 4's rulings missing from the spec and plan | Spec Appendix C gains a slice 4 group; plan corrected |
| Efficiency | Integration tier at 208 against a 205 target | Three landing tests on FakeGit; one real-git smoke test keeps the spaces-in-path checkout |

## Exceptions and carries

- No production change in the fix batch; the decision trace is unchanged against 4d8039d throughout slice 4.
- Safety fixes the slice made on the way, each now pinned: a cherry that could not run no longer leads cleanup to rescue and delete a branch; a failed worktree listing no longer lets origin's compare-and-swap move main under a live checkout; a relock that times out journals `failed`.
- Pre-merge mutant gate keeps: `no=(0,)` ordering, GitFailed's `exit N` text, env passthrough without the pointer env, `__reduce__`.
- Carried to the final review: `disk_registrations.protect` raises before writing its locks; a stale `index.lock` after a merge timeout reads as MainMoved on retry.
- Carried to Task 43: the relock-also-times-out row must carry both errors (spec line 900). To Task 45: the `answer_spawns` fake shape and direct `process.subprocess.run` fakes.
