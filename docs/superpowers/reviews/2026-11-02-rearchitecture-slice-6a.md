# Slice 6a: `disk_evidence` whole

`SLICE_BASE`: 11568c2. Commits: Tasks 37, 38 and 39.

## What the slice changed

- Task 37: the path facts (`repository_config`, `repository_root`, `safe_path`, `worktree_roots`) moved from `disk_paths` to `disk_inventory`, so `disk_inventory` no longer imports `disk_paths`. `repository_config` retains on a failed question only.
- Task 38: every evidence read moved into `StageEvidence` (`measure`, `status`, `head`, `gitdir`, `merged`, `tag`, `tracked`, `declarations`, `task_facts`, `fresh_snapshot`), and each git read takes `clock.patience(cap)`. `identity`, `registration_identity`, `outputs_at` and `snapshot` became module functions here.
- Task 39: removal needs a fresh proof that only `disk_evidence` can make. `FreshRegistrations(refused, overlaps)` and `FreshProcesses(proof)` refuse to be built without the module's private token. `StageEvidence.fresh_registrations(repo, target, own, *, pointer)` is now the only caller of `git_worktrees.prove`. Its timeout is `clock.patience(PROOF_TIMEOUT_S)`, and it computes `overlaps` from `Proof.reach`. `fresh_processes()` always takes a new snapshot, and `snapshot` uses it. `overlaps_worktrees` is gone: its body is now `disk_evidence.overlaps(target, own, reach)`, which the first pass in `disposable_rows` uses against `every_registered()`. The mutable `Listings` no longer leave `StageEvidence`, because the `listings` property is removed.

No decision trace row changed.

## Exit checks

| Check | Command | Result |
|---|---|---|
| Trace prints nothing | `git diff --exit-code 11568c2 -- tests/disk_decision_trace.json` | no output, rc 0 |
| R9's caller is `disk_evidence` alone | `R9_CALLERS = ('drover/disk_evidence.py',)`. `grep -rn "git_worktrees.prove\|import prove\|\bprove(" drover hub` outside `git_worktrees` | only `drover/disk_evidence.py:135`. `tests.test_seams` OK |
| `disk_inventory` imports no `disk_paths` | `grep -n disk_paths drover/disk_inventory.py` | no match, rc 1 |
| Row 6a's symbols removed | grep each of the 8 names in `tests/architecture_removed_symbols.json` | each 1. `tests.test_architecture` OK |
| `disk_evidence` whole | the section 7.1 interface plus `overlaps` | `tests.test_disk_evidence` 23 OK |
| Verify | `DROVER_TESTS=integration $PY -m unittest $M $GUARDS` (M as in Task 39 Step 4) | `Ran 197 tests in 115.173s` `OK (skipped=2)` |

## Mutant checks for the lead, at slice end

Each mutant must be caught by the named test.

- Remove `_Fresh.__post_init__`'s token check: `test_disk_evidence.FreshProofs.test_constructors_refuse_without_the_token`.
- `fresh_processes` returns the cached first-pass proof: `Processes.test_fresh_processes_is_always_a_new_snapshot`.
- `snapshot` calls `processes()`: `Reads.test_snapshot_takes_fresh_processes_never_processes`.
- `fresh_registrations` caches its first proof, or passes a fixed `PROOF_TIMEOUT_S`: `FreshProofs.test_proof_timeout_is_patience`.
- `overlaps` is computed against `()` instead of `proof.reach`: `FreshProofs.test_overlaps_comes_from_proof_reach_as_overlaps_worktrees_did`, `test_disk_fresh_proof.FreshProof.test_stage_4_measures_nesting_against_what_the_proof_read`, and `test_disk_outputs.*.test_apply_still_revalidates_registrations_fresh`.
- `overlaps` drops `other != own_parts`, or drops one prefix direction: `FreshProofs.test_overlaps_…` and `test_disk_removal.RegisteredListing.test_overlaps_measures_its_reach_and_never_lists`.
- `if proof.overlaps` in `disk_worktrees.remove` becomes `False`: `test_stage_4_measures_nesting_against_what_the_proof_read`. The same mutant in `disk_paths.remove_output`: `test_apply_still_revalidates_registrations_fresh`.
- `pointer=` forced to True in `disk_worktrees.remove`: `test_disk_fresh_proof.FreshProof.test_a_missing_candidate_and_a_stray_expect_no_pointer`.
- The first-pass `overlaps(...)` in `disposable_rows` becomes `False`: the trace scene `output_nested` (`tests.test_disk_decision_trace`).

## What the slice-end reviewer must know

- `StageEvidence(prove=None)` departs from 7.1's `prove=git_worktrees.prove`. With None, `git_worktrees.prove` is looked up when each proof is taken, so the fixtures that patch `git_worktrees.prove` (`disk_fixture.agreeing_proof`, `disk_memory`, `test_disk_fresh_proof`, `test_disk_outputs`) still reach the stage's proof.
- `disk_evidence.overlaps` is a public pure function that section 7.1 does not list. `disposable_rows`' first pass and `fresh_registrations` share it, and slice 6b's `rule_unit` is meant to use it. The R6 row for `disposable_rows` now names a deferred import of `disk_evidence` (the load cycle), in place of `disk_worktrees`.
- Callers outside the brief's file list were changed for `FreshProcesses`: `drover/disk_temp.py` (`.proof`) and `tests/disk_memory.py`'s fake `snapshot`. Until slice 6b's `rule_*_fresh` takes the wrapper itself, each caller unwraps it at the call site.
