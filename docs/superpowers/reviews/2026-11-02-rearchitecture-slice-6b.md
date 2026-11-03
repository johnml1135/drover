# Slice 6b: `disk_authority`

`SLICE_BASE`: 2deb6b2. Commits: Tasks 40 (8a5bee7), 41 (6fe1130) and 42.

## What the slice changed

- Task 40: `decide` and `decide_and_store` moved, byte for byte, into `disk_authority`. `CleanupState` moved into `disk_state`. `disk_cleanup_decisions` keeps `CleanupRuntime` and `evaluate`, and imports from `disk_authority` at load.
- Task 41: the disposable-path rules moved: `rule_disposable`, `rule_disposable_fresh`, `rule_unit`, `rule_unit_fresh`, `DisposableUnit`, `protected_path`, `repository_tree`, `live_tree`, `disposable_match` and `ignored_outside`. `output_evidence` is gone. Each refusal is now a keep `Ruling`.
- Task 42: the worktree, quarantine, temp and cache rules moved.
  - Into `disk_authority`: `inspect(run, machine, registration, *, processes)`, `quarantine_observation(run, machine, row)`, `quarantine(run, machine, row, *, store=None)`, `unlisted_live(run, path)`, `needs_idle_gate(run, tag)` and `CLOSED_IDLE_S`.
  - New rules: `rule_temp_entry(run, path)` holds `temp_facts`' static checks, and `rule_cache_file(run, path, info, *, entry)` holds the folder cache's age and level check.
  - What stays in the stage modules: `disk_temp` keeps the scan, its cache and the per-root budget, and `temp_facts` raises `ValueError(ruling.detail)` on a keep, as it did before. `disk_paths.folder_cache` asks `rule_cache_file`. `disk_worktrees` keeps `settled`, `unlisted`, `registrations`, `WorktreeEvidence`, `remove` and the git steps; `remove` and the git steps go in 6c.
  - `disk_worktrees` now imports `disk_cleanup_decisions` and `disk_registrations` at load, so its two R6 rows are gone. R6 falls from 9 statements to 7.

No decision trace row changed.

## Exit checks

| Check | Command | Result |
|---|---|---|
| Trace prints nothing | `git diff --exit-code 2deb6b2 -- tests/disk_decision_trace.json`, and the same against 11568c2 | no output, rc 0, both |
| Every rule of the 7.2 map is in `disk_authority` | `grep -nE "^(def\|class) (decide\|decide_and_store\|inspect\|quarantine_observation\|quarantine\|unlisted_live\|needs_idle_gate\|rule_[a-z_]+\|protected_path\|repository_tree\|live_tree\|disposable_match\|ignored_outside\|DisposableUnit\|Ruling)\b\|^CLOSED_IDLE_S" drover/*.py` | every hit is in `drover/disk_authority.py`, apart from the unrelated `drover/decide.py:328 decide(name, state, ...)` |
| Authority import rule live | `tests.test_seams` R5 row for `disk_authority` (no `process`, no `git_client`). `disk_authority` imports `disk_evidence`, `placement`, `disk_inventory`, `disk_stage` and `disk_state`, and `grep -l disk_authority` over those files finds nothing, so nothing there imports back | OK, no match |
| The paths-worktrees cycle is gone | `grep -nE "disk_worktrees\|disk_paths" drover/disk_paths.py drover/disk_worktrees.py` | no match. A fresh-interpreter import of disk_side, config, disk_worktrees, disk_preflight, disk, disk_cleanup, disk_cleanup_decisions, disk_evidence, placement_worker and disk_authority raises nothing |
| Row 6b's symbols removed | `tests/architecture_removed_symbols.json` holds `disk_paths.output_evidence`, `disk_cleanup_decisions.decide`, `disk_cleanup_decisions.CleanupState`, `disk_worktrees.inspect` and `disk_worktrees.quarantine` | each present. `tests.test_architecture` OK |
| R6 count falls | `"rule": "R6"` rows in `tests/seam_exceptions.json` | 8 at 6fe1130, 6 now |
| Verify (Task 42 Step 4's `M`, in two chunks under 170 s) | `DROVER_TESTS=integration $PY -m unittest <M part> $GUARDS` | `Ran 213 tests in 57.426s` `OK (skipped=5)`; `Ran 98 tests in 105.672s` `OK` |

## Mutant checks for the slice gate

Each mutant below should be caught by the test named. None has been run.

From Task 41 (task-41-report.md):
1. `_disposable`: any one `return reject/_unavailable` replaced by `pass` (eight refusals). Caught by `test_disk_authority.DisposableRules.test_a_rule_returns_a_ruling_and_never_raises_to_mean_no`, and by the trace scenes `output_rejected` and `output_kept`.
2. The idle waiver with `request.budget or` dropped, or with `('hold','critical')` widened. Caught by `test_idle_waiver_moved_verbatim`, and by the trace scenes `budget_idle_at_critical` and `output_idle_*`.
3. `rule_disposable_fresh` with its isinstance check removed, or with `processes.proof` replaced by `run.evidence.processes()`. Caught by `test_fresh_rule_refuses_a_first_pass_proof`, and by the trace scene `goes_busy`.
4. `rule_unit_fresh` with any one of these removed: the refused branch, `proof.overlaps`, the `repository_tree` any, `cleanup_contains`, `live_tree`, or the isinstance check. Caught by `test_rule_unit_fresh_reads_overlaps_and_refused`, the trace scenes, and `test_disk_fresh_proof`.
5. The callers' checks, each negated or removed: `if fresh.action != 'candidate'` and `if ruling.action != 'candidate'` in `remove_output`, and `if judged.action != 'candidate'` in `disposable_rows`. Caught by the trace scenes `output_*`.
6. `rule_unit` with the `drive_unproven`, `live_owner` or overlap return removed, or with the `else: row['reason_code']` branch made a candidate. Caught by the trace scenes `output_*`.

Task 42:
7. `inspect`: any one early `return decision()` removed (other_side, other_drive, main, busy, protected, missing, live, dirty). Caught by the trace scenes for each `reason_code` in `decide`, and by `test_inspect_requires_processes` for busy.
8. `inspect`'s `processes` given a default. Caught by `test_inspect_requires_processes`.
9. `inspect` reading `evidence.head` before the `missing` return. Caught by `test_missing_row_keeps_head_from_the_listing_and_needs_no_tip`.
10. `needs_idle_gate` with `!= 'landed'` flipped, or `< CLOSED_IDLE_S` changed to `<=`, or the gate dropped from `inspect`. Caught by `test_needs_idle_gate_goes_only_after_three_days_quiet` and `tests.test_disk_yanked`.
11. `WorktreeEvidence.observe` taking the proof at once (`run.evidence.processes()` in place of `LazyProof(...)`). Caught by `test_stage_4_takes_no_process_proof_for_rows_its_path_checks_keep`; this one was seen failing by hand. If `LazyProof` takes its proof at construction: `test_a_lazy_proof_is_taken_only_past_the_path_checks`.
12. `remove`'s fresh inspect given the first-pass proof (`LazyProof(evidence.processes)`). Caught by the trace scene `revalidation_busy`.
13. `quarantine_observation`: `and not live` dropped, `excluded` forced to `[]`, or `identity` forced to None. `quarantine`: `_view(run)` with apply forced to False. Caught by the trace `SEQUENCES` (`quarantine*`) and `tests.test_disk_quarantine`.
14. `unlisted_live` with its lease clause dropped. Caught by the trace scenes `unlisted_live` and `quarantine_unlisted_goes_live`.
15. `rule_temp_entry` with any one of its conditions dropped (`safe_path`, `live_tree`, a protected root either way, a repository above). Caught by `test_rule_temp_entry_holds_only_the_static_checks`. `temp_facts`' `!= 'candidate'` check negated or removed: caught by the trace's temp scenes and `tests.test_disk_temp`.
16. `rule_cache_file` with `mode != 'daily'` dropped, a level removed, `>` changed to `>=`, or the 30-day default changed. Caught by `test_rule_cache_file_age_and_level`. `folder_cache`'s `== 'candidate'` check negated: caught by `tests.test_disk_caches` and the trace's cache scenes.

## What the slice-end reviewer must know

- `processes` is required, as 7.2 says. `LazyProof(take)` in `disk_authority` is what the two callers pass. It is taken when `inspect` reads `busy`, which is where `evidence.processes()` and `fresh_processes()` were called before. So a row that its path checks keep still takes no proof. If the proof were taken eagerly, a stage whose rows are all on the other side would scan processes, and `process_rows` would add report rows.
- `rule_cache_file` takes a keyword-only `entry`, beyond 7.2's `(run, path, info)`, because the age limit is the cache entry's `max_age_days`. A cache path can sit under two nested entries, so finding the entry from `path` could pick the wrong limit.
- `remove`, `unlisted` and `WorktreeEvidence` now take `run=` in place of `clock=` and `evidence=`, because `inspect` and `quarantine` read the run. The test harness's `at_removal` and `disk_fixture.stage_request` (which now returns the run) changed to match.
- One `dynamic_entrypoints` allowlist row was renamed one for one: `drover/disk_worktrees.py getattr proof, 'reason'` became `drover/disk_authority.py getattr processes, 'reason'`.
- `decide` and `decide_and_store` are given `dict(apply, now, approved_path=None)`, the only keys they read, so `decide_view()` is not copied once per worktree.
