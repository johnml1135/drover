# Rearchitecture test times

Baseline B for every later target in the plan's Test Time section. Seconds come from `scripts/test.py -j 4` in the default tier; spawns come from a census. Each checkpoint appends its row below the baseline by the same method.

## Baseline

Taken on 7dae154, which differs from 4bcb1c8 only in the plan and the design. Base confirmation: the unittest discover run at 4bcb1c8 passed, 4020 tests with 42 skipped in 1627 s. It covers both tiers, one process, and gives no per-module seconds, so the one extra default-tier run the plan allows was made:

```bash
$PY scripts/test.py -j 4 --durations 10
```

It ended `Ran 3811 tests (26 skipped) in 266 modules, wall 281.7s, 0 failed modules`. The 3811 default-tier tests and the 209 integration tests make the 4020.

### Totals

| Set | Modules | Seconds | Spawns |
|---|---:|---:|---:|
| All modules | 266 | 1121.1 | 3832 |
| In `tests.IMPURE` | 65 | 525.6 | 3780 |
| Outside `tests.IMPURE` | 201 | 595.5 | 52 |
| `test_disk_*` | 55 | 315.3 | 742 |

- **Suite total:** 281.7 s wall for the default tier, over 292 jobs. Module seconds sum to 1121.1 s, which is 3.98 workers busy throughout.
- **`-j`:** 4, on 20 logical processors.
- **Load at the time:** not idle. No other suite or Drover test process ran, but total processor time read 96.7% to 100.0% (mean 98.9%, 9 samples at 30 s) and available memory 7.1 to 10.0 GB of 63.7 GB. With no suite running, at 14:39, it still read 65% to 92%, with `vmmemwsl` on about 6 cores and `rustc` on about 4. These are other work on the machine and were not stopped. So B's seconds are load-bound and likely high; spawns are the stable measure.
- **Sharding:** the timings file held one entry at the start, so only `SHARD_LIMITS` and `HEAVY` modules were split. A later run reads this run's timings and may split more modules, which adds process start-up to a module's seconds.

### Method

The census lives outside the repo. Each module runs in its own process with `DROVER_TESTS` unset, the same environment `scripts/test.py` sets, through `tests.tiers.TierLoader`, with a counter on `subprocess.Popen.__init__` and `os.system`. Every module was counted, not only `tests.IMPURE` and `mods 'placement|disk_|git'`, so the zeros are measured. Two full runs agreed on every module.

- A row's spawns are all calls in that module's process, keyed by `tests._caller_module()` or on a thread where that returns `None` (28 calls, in `test_hub_api`, `test_hub_dispatch`, `test_hub_ui` and `test_task_views`).
- The counter sits outside the purity guard, so a refused attempt counts: the 2 in `test_guards` are its own self-test and start nothing.
- A fake that replaces `subprocess.run` or `Popen` is not counted, and neither is a grandchild of a spawned process.
- Of the 3832, 1292 were issued from code in `tests/` and 2540 from `drover/`, `hub/` or `scripts/`.

### Purity gap: modules outside `IMPURE` that spawn

The plan expected 0 spawns outside `tests.IMPURE`. Two modules really do spawn there:

| Module | Spawns | Inherits from |
|---|---:|---|
| `test_wsl_worktree` | 35 | `test_disk_worktrees` |
| `test_config_overlay` | 15 | `test_hub_runtime` |

`_caller_module` returns the innermost `tests.test_*` frame, so a spawn in a base class or helper imported from an `IMPURE` module counts as that module's. The default tier's guard therefore passes both modules, and `tests.IMPURE` understates what spawns.

The same rule works the other way: 9 modules in `tests.IMPURE` spawn nothing themselves. They are `test_allowance`, `test_decide`, `test_decide_devices`, `test_disk_worktrees`, `test_hub_api_listener`, `test_hub_api_tasks`, `test_landing`, `test_landing_repository` and `test_tool_discovery`. Some host base classes that other modules use, and taking such a host off the list makes its importers fail. `tests.IMPURE` also admits real sleeps, as in `test_decide`, so 0 spawns does not mean a module is free to leave the list. None of this was changed here.

### Conversion candidates: top 15 by spawns

These 15 modules make 2752 of the 3832 spawns (72%). Test-side spawns come from fixture code in `tests/`. Production-side spawns come through `drover/`, `hub/` or `scripts/`, which can still be setup, so the last column is a reading of the module and not a measurement. `test_wizard` and `test_disk_state` tie at 90.

| Rank | Module | Spawns | Test side | Production side | Setup or point |
|---:|---|---:|---:|---:|---|
| 1 | `test_hub_runtime` | 520 | 102 | 418 | Point. Real git export through `hub/runtime`, which ruling 15 freezes, so no `process` or `git_client` seam reaches it. |
| 2 | `test_garden_branch` | 280 | 167 | 113 | Mixed. Fixture branches are setup; rescue refs and bundles on real refs are the point. |
| 3 | `test_origin_sync` | 234 | 134 | 100 | Mixed. Remote fixtures are setup, and the fetch, push and compare-and-swap behaviour is close to the point. A slice 4 candidate. |
| 4 | `test_hub_runtime_paths` | 233 | 33 | 200 | Point, as rank 1. See the note below. |
| 5 | `test_notification_fences` | 206 | 74 | 132 | Setup. The subject is STOP barriers; `LandingCase` repos are scenery. |
| 6 | `test_garden` | 205 | 71 | 134 | Setup. Stage 4 selection over `WorktreeCase` repos; a slice 6 `StageEvidence` candidate, though it is not named in the plan. |
| 7 | `test_hub_runtime_integrity` | 183 | 19 | 164 | Point, as rank 1. |
| 8 | `test_disk_worktree_add` | 150 | 30 | 120 | Mixed. Tagging a real linked checkout is the point; `DiskCase` in three classes and `LandingCase` in one are setup. |
| 9 | `test_disk_report` | 134 | 34 | 100 | Setup. Two tests over `WorktreeCase`; a slice 6 candidate. |
| 10 | `test_disk_policy` | 133 | 58 | 75 | Setup. Committed disposable paths are read through real git; a slice 4 candidate. |
| 11 | `test_config_migration` | 102 | 48 | 54 | Setup. `RepoCase` plus the repository loader's git reads; not named in the plan. |
| 12 | `test_repo_scopes` | 100 | 27 | 73 | Setup, as rank 11; not named in the plan. |
| 13 | `test_deploy_hub` | 92 | 27 | 65 | Point. Moving a deploy between real commits; service and process steps are already injected. |
| 14 | `test_wizard` | 90 | 7 | 83 | Setup. 16 of 17 `git init` calls come from `decisions.initialize`, a side effect of the flow under test. |
| 15 | `test_disk_state` | 90 | 27 | 63 | Setup, except the Python helpers in the cross-process lock test, which are the point. |

Note on ranks 1, 4 and 7: together they are 936 spawns, 24% of B, and sit behind `hub/runtime`'s local `git`. The plan's Test Time table lists `test_hub_runtime_paths` as a slice 4 candidate for process-level fakes of `git_client`, but ruling 15 keeps that module off `git_client`. It can convert only by faking `hub/runtime`'s own call, or it stays, with its reason in the slice report.

Four other modules the plan's table names have no default-tier spawns to remove: `test_landing_repository` has no default-tier tests, and `test_disk_worktrees`, `test_disk_registration_timeouts` and `test_disk_settled_deadline` show 0 spawns. Their own tests cannot lower B, though `test_disk_worktrees` hosts `WorktreeCase`, whose spawns count under its importers.

### Per module

Sorted by spawns, then seconds. Tests and seconds are for the default tier; seconds sum a module's shards. A module with integration tests shows how many it holds; those are not run here.

| Module | Tests | Seconds | Spawns | Tier | In `IMPURE` |
|---|---:|---:|---:|---|---|
| `test_hub_runtime` | 13 | 42.9 | 520 | default | yes |
| `test_garden_branch` | 15 | 25.3 | 280 | default | yes |
| `test_origin_sync` | 11 | 21.9 | 234 | default | yes |
| `test_hub_runtime_paths` | 11 | 20.5 | 233 | default | yes |
| `test_notification_fences` | 17 | 23.0 | 206 | default | yes |
| `test_garden` | 4 | 22.2 | 205 | default | yes |
| `test_hub_runtime_integrity` | 10 | 17.7 | 183 | default | yes |
| `test_disk_worktree_add` | 5 | 17.1 | 150 | default | yes |
| `test_disk_report` | 2 | 14.0 | 134 | default | yes |
| `test_disk_policy` | 6 | 10.0 | 133 | default | yes |
| `test_config_migration` | 9 | 8.3 | 102 | default | yes |
| `test_repo_scopes` | 4 | 7.7 | 100 | default | yes |
| `test_deploy_hub` | 19 | 9.3 | 92 | default | yes |
| `test_wizard` | 26 | 11.1 | 90 | default | yes |
| `test_disk_state` | 16 | 9.4 | 90 | default | yes |
| `test_config_explain` | 5 | 7.2 | 87 | default | yes |
| `test_wizard_scopes` | 4 | 7.2 | 84 | default | yes |
| `test_disk_ownership` | 6 | 7.7 | 80 | default | yes |
| `test_decisions` | 9 | 8.9 | 74 | default | yes |
| `test_task_access` | 15 | 8.8 | 68 | default | yes |
| `test_wizard_detection` | 30 | 9.1 | 64 | default | yes |
| `test_disk_ci_admission` | 2 | 5.3 | 63 | default | yes |
| `test_repos` | 11 | 7.6 | 62 | default | yes |
| `test_repo_entry` | 7 | 4.4 | 51 | default | yes |
| `test_config_layout` | 3 | 4.1 | 47 | default | yes |
| `test_task_views` | 21 | 9.9 | 45 | default | yes |
| `test_placement` | 18 | 6.8 | 43 | default | yes |
| `test_disk_branch` | 2 | 3.6 | 42 | default | yes |
| `test_hub_ops` | 18 | 7.8 | 40 | default | yes |
| `test_wsl_worktree` | 12 | 4.1 | 35 | default | no |
| `test_dispatcher_impure` | 24 | 6.0 | 27 | default | yes |
| `test_hub_ui` | 10 | 6.8 | 25 | default | yes |
| `test_disk_fix7` | 22 | 5.4 | 22 | default | yes |
| `test_hub_api` | 28 | 8.7 | 15 | default | yes |
| `test_config_overlay` | 2 | 2.1 | 15 | default | no |
| `test_hub_dispatch` | 30 | 6.7 | 13 | default | yes |
| `test_delete_regressions` | 9 | 4.6 | 13 | default | yes |
| `test_disk_caches` | 7 | 6.0 | 12 | default | yes |
| `test_disk_fix8` | 35 | 3.0 | 9 | default | yes |
| `test_hub_ops_controls` | 22 | 6.5 | 6 | default | yes |
| `test_resources` | 45 | 2.0 | 6 | default | yes |
| `test_launch_runner_handle` | 22 | 5.4 | 4 | default | yes |
| `test_tuner` | 36 | 5.1 | 4 | default | yes |
| `test_usage` | 27 | 3.8 | 3 | default | yes |
| `test_artifact_records` | 4 | 2.2 | 3 | default | yes |
| `test_disk_trigger` | 15 | 11.3 | 2 | default | yes |
| `test_notification_acceptance` | 5 | 4.6 | 2 | default | yes |
| `test_guards` | 12 | 2.5 | 2 | default | no |
| `test_proc_scan` | 2 | 1.6 | 2 | default | yes |
| `test_disk_trim` | 3 | 26.8 | 1 | default | yes |
| `test_disk_fixes` | 10 | 8.6 | 1 | default | yes |
| `test_disk_fix6` | 14 | 3.8 | 1 | default | yes |
| `test_waiting_migration` | 9 | 2.2 | 1 | default | yes |
| `test_type_yaml` | 4 | 1.6 | 1 | default | yes |
| `test_reload_protocol` | 23 | 1.4 | 1 | default | yes |
| `test_launch_runner` | 20 | 1.1 | 1 | default | yes |
| `test_disk_boundaries` | 2 | 1.0 | 1 | default | yes |
| `test_disk_lock_wrapper` | 1 | 0.8 | 1 | default | yes |
| `test_task_refresh` | 1 | 0.5 | 1 | default | yes |
| `test_disk_budget_folders` | 28 | 22.8 | 0 | default | no |
| `test_disk_budget` | 32 | 20.2 | 0 | default | no |
| `test_cleanup_throttle` | 26 | 20.0 | 0 | default | no |
| `test_disk_outputs` | 12 | 17.9 | 0 | both (2 integration) | no |
| `test_waiting_structure` | 8 | 14.5 | 0 | default | no |
| `test_disk_budget_coordinator` | 17 | 14.4 | 0 | default | no |
| `test_disk_notices` | 9 | 12.0 | 0 | default | no |
| `test_disk_quarantine` | 4 | 10.5 | 0 | both (1 integration) | no |
| `test_disk_budget_processes` | 22 | 9.8 | 0 | default | no |
| `test_disk_fix3` | 9 | 9.5 | 0 | default | no |
| `test_hub_api_listener` | 11 | 9.2 | 0 | default | yes |
| `test_pipeline_review` | 23 | 8.4 | 0 | default | no |
| `test_disk_worktrees` | 3 | 8.0 | 0 | both (1 integration) | yes |
| `test_pipeline_safety_commit` | 25 | 7.4 | 0 | default | no |
| `test_architecture` | 4 | 7.2 | 0 | default | no |
| `test_disk_temp` | 5 | 6.8 | 0 | default | no |
| `test_platforms` | 25 | 6.6 | 0 | default | no |
| `test_notification_api` | 14 | 6.4 | 0 | default | no |
| `test_disk_fix2` | 13 | 6.3 | 0 | both (2 integration) | no |
| `test_pane_compaction` | 39 | 6.1 | 0 | default | no |
| `test_pane_resume_handoff` | 33 | 5.9 | 0 | default | no |
| `test_dispatch_repos` | 13 | 5.8 | 0 | default | no |
| `test_pane_send` | 51 | 5.6 | 0 | default | no |
| `test_compaction` | 41 | 5.5 | 0 | default | no |
| `test_web_perf` | 18 | 5.4 | 0 | default | no |
| `test_disk_scope` | 2 | 5.4 | 0 | default | no |
| `test_seat_paths_contract` | 1 | 5.4 | 0 | default | no |
| `test_disk_fix5` | 10 | 5.1 | 0 | both (6 integration) | no |
| `test_panes_lapsed` | 37 | 5.1 | 0 | default | no |
| `test_batch_approve` | 17 | 5.1 | 0 | default | no |
| `test_replan_batches_recovery` | 17 | 5.1 | 0 | default | no |
| `test_pane_rotation` | 35 | 5.1 | 0 | default | no |
| `test_pipeline_worktrees` | 18 | 5.0 | 0 | default | no |
| `test_panes` | 31 | 5.0 | 0 | default | no |
| `test_disk_fix4` | 4 | 4.9 | 0 | both (1 integration) | no |
| `test_compaction_hub` | 26 | 4.7 | 0 | both (2 integration) | no |
| `test_pane_launch_stall` | 52 | 4.5 | 0 | default | no |
| `test_panes_runtime` | 38 | 4.5 | 0 | default | no |
| `test_pane_handoff` | 35 | 4.5 | 0 | default | no |
| `test_pane_send_startup` | 58 | 4.4 | 0 | default | no |
| `test_pane_compaction_kept` | 27 | 4.4 | 0 | default | no |
| `test_notification_api_delivery` | 8 | 4.4 | 0 | default | no |
| `test_bypass_hub` | 17 | 4.4 | 0 | default | no |
| `test_replans` | 15 | 4.3 | 0 | default | no |
| `test_pane_launch` | 49 | 4.3 | 0 | default | no |
| `test_replan_batches_dispatch` | 12 | 4.3 | 0 | both (1 integration) | no |
| `test_notification_api_replay` | 7 | 4.3 | 0 | default | no |
| `test_pipeline_shared_worktrees` | 20 | 4.2 | 0 | default | no |
| `test_replan_batches_ledger` | 19 | 4.0 | 0 | default | no |
| `test_pane_send_rules` | 14 | 4.0 | 0 | default | no |
| `test_landing` | 32 | 3.9 | 0 | default | yes |
| `test_outside_land_answers` | 44 | 3.9 | 0 | default | no |
| `test_pane_wake_states` | 33 | 3.9 | 0 | default | no |
| `test_waiting_consumers_lifecycle` | 13 | 3.9 | 0 | default | no |
| `test_pipeline_basics` | 13 | 3.8 | 0 | default | no |
| `test_ci_outcome_contract` | 15 | 3.7 | 0 | default | no |
| `test_panes_brief` | 28 | 3.7 | 0 | default | no |
| `test_hub_api_tasks` | 12 | 3.7 | 0 | default | yes |
| `test_bypass` | 38 | 3.6 | 0 | default | no |
| `test_pane_send_guards` | 41 | 3.6 | 0 | default | no |
| `test_outside_land_limits` | 31 | 3.6 | 0 | default | no |
| `test_hub_stats` | 8 | 3.6 | 0 | default | no |
| `test_ledger` | 38 | 3.5 | 0 | default | no |
| `test_waiting_characterization` | 10 | 3.5 | 0 | default | no |
| `test_pipeline_contract_landing` | 7 | 3.5 | 0 | both (3 integration) | no |
| `test_disk_hub` | 4 | 3.5 | 0 | default | no |
| `test_ci_policy` | 12 | 3.4 | 0 | default | no |
| `test_ledger_workflow` | 27 | 3.4 | 0 | default | no |
| `test_director_roles` | 31 | 3.3 | 0 | default | no |
| `test_directors` | 31 | 3.3 | 0 | default | no |
| `test_disk_cleanup` | 4 | 3.3 | 0 | default | no |
| `test_pane_wake` | 28 | 3.2 | 0 | default | no |
| `test_pane_faults` | 25 | 3.2 | 0 | default | no |
| `test_replans_dispatch` | 8 | 3.1 | 0 | default | no |
| `test_dispatcher_recovery` | 18 | 3.0 | 0 | default | no |
| `test_dispatch_setting_hub` | 6 | 2.9 | 0 | default | no |
| `test_bypass_runs` | 20 | 2.8 | 0 | default | no |
| `test_disk_temp_budget` | 6 | 2.7 | 0 | default | no |
| `test_landing_recovery` | 29 | 2.6 | 0 | default | no |
| `test_waiting_consumers` | 11 | 2.5 | 0 | both (2 integration) | no |
| `test_bypass_admission` | 18 | 2.5 | 0 | default | no |
| `test_hub_web` | 19 | 2.5 | 0 | default | no |
| `test_disk_wsl_used` | 34 | 2.5 | 0 | default | no |
| `test_pane_close` | 24 | 2.4 | 0 | default | no |
| `test_pane_close_safety` | 23 | 2.4 | 0 | default | no |
| `test_panes_hub` | 14 | 2.4 | 0 | default | no |
| `test_pane_rotation_start` | 15 | 2.4 | 0 | default | no |
| `test_notifications` | 22 | 2.3 | 0 | default | no |
| `test_gate_scheduling` | 18 | 2.3 | 0 | default | no |
| `test_importer` | 16 | 2.2 | 0 | default | no |
| `test_worker_continuation` | 13 | 2.2 | 0 | default | no |
| `test_dispatcher_admission` | 19 | 2.2 | 0 | default | no |
| `test_tree` | 4 | 2.2 | 0 | default | no |
| `test_waiting_releases` | 14 | 2.1 | 0 | default | no |
| `test_disk_schedule` | 6 | 2.1 | 0 | default | no |
| `test_pane_compaction_job` | 16 | 2.1 | 0 | default | no |
| `test_ci_slots` | 25 | 2.1 | 0 | default | no |
| `test_panes_launches` | 17 | 2.1 | 0 | default | no |
| `test_panes_blocked` | 15 | 2.0 | 0 | default | no |
| `test_dispatch_limits` | 11 | 2.0 | 0 | default | no |
| `test_authority_output` | 5 | 2.0 | 0 | default | no |
| `test_authority_http` | 2 | 2.0 | 0 | default | no |
| `test_disk_progress` | 3 | 2.0 | 0 | default | no |
| `test_config` | 42 | 1.9 | 0 | default | no |
| `test_notification_groups` | 16 | 1.9 | 0 | default | no |
| `test_seats` | 28 | 1.9 | 0 | default | no |
| `test_first_dispatch_homes` | 11 | 1.9 | 0 | default | no |
| `test_outside_land_recording` | 18 | 1.9 | 0 | default | no |
| `test_first_dispatch_admission` | 14 | 1.9 | 0 | default | no |
| `test_watch` | 20 | 1.9 | 0 | default | no |
| `test_waiting` | 11 | 1.8 | 0 | default | no |
| `test_stats` | 20 | 1.8 | 0 | default | no |
| `test_decide` | 37 | 1.8 | 0 | default | yes |
| `test_dispatcher_hardening` | 16 | 1.8 | 0 | default | no |
| `test_ci_diagnosis` | 9 | 1.8 | 0 | default | no |
| `test_dispatcher_launch` | 13 | 1.8 | 0 | default | no |
| `test_hub_watchdog` | 10 | 1.8 | 0 | default | no |
| `test_importer_identity` | 13 | 1.7 | 0 | default | no |
| `test_pane_rollback` | 13 | 1.7 | 0 | default | no |
| `test_hub_outside` | 1 | 1.7 | 0 | default | no |
| `test_dispatch_hold` | 26 | 1.6 | 0 | default | no |
| `test_ops_panes` | 16 | 1.6 | 0 | default | no |
| `test_notification_controls` | 15 | 1.5 | 0 | default | no |
| `test_waiting_migration_questions` | 9 | 1.5 | 0 | default | no |
| `test_pane_exit` | 12 | 1.5 | 0 | default | no |
| `test_handoff` | 38 | 1.4 | 0 | default | no |
| `test_dispatch_setting` | 12 | 1.4 | 0 | default | no |
| `test_disk_retirement` | 1 | 1.4 | 0 | default | no |
| `test_first_dispatch_setup` | 18 | 1.3 | 0 | default | no |
| `test_replans_records` | 1 | 1.3 | 0 | both (1 integration) | no |
| `test_hub_port_status` | 2 | 1.3 | 0 | default | no |
| `test_lifecycle_characterization` | 8 | 1.2 | 0 | default | no |
| `test_pipeline` | 0 | 1.2 | 0 | default | no |
| `test_ledger_outside` | 8 | 1.2 | 0 | default | no |
| `test_pane_launch_start` | 7 | 1.2 | 0 | default | no |
| `test_actions` | 8 | 1.1 | 0 | default | no |
| `test_task_context` | 6 | 1.1 | 0 | default | no |
| `test_scope_base` | 0 | 1.1 | 0 | integration only | no |
| `test_disk_linux_schedule` | 3 | 1.1 | 0 | default | no |
| `test_replan_batches` | 0 | 1.1 | 0 | default | no |
| `test_disk_wsl_outputs` | 7 | 1.0 | 0 | default | no |
| `test_state_dir` | 10 | 1.0 | 0 | default | no |
| `test_compaction_hold` | 22 | 0.9 | 0 | default | no |
| `test_landing_diagnosis` | 7 | 0.9 | 0 | default | no |
| `test_authority_slice` | 4 | 0.9 | 0 | default | no |
| `test_read_acceptance` | 5 | 0.9 | 0 | default | no |
| `test_type_contract` | 18 | 0.8 | 0 | default | no |
| `test_decide_devices` | 24 | 0.8 | 0 | default | yes |
| `test_disk_config` | 5 | 0.8 | 0 | default | no |
| `test_allowance` | 21 | 0.8 | 0 | default | yes |
| `test_context_files` | 16 | 0.8 | 0 | default | no |
| `test_dispatcher` | 1 | 0.8 | 0 | default | no |
| `test_archive_regressions` | 3 | 0.8 | 0 | default | no |
| `test_gate_resume` | 4 | 0.8 | 0 | default | no |
| `test_launch` | 22 | 0.7 | 0 | default | no |
| `test_disk_fix9` | 11 | 0.7 | 0 | default | no |
| `test_pane_rule_coverage` | 2 | 0.7 | 0 | default | no |
| `test_outside_land` | 0 | 0.6 | 0 | integration only | no |
| `test_outside_land_decider` | 0 | 0.6 | 0 | integration only | no |
| `test_approve_cli` | 9 | 0.6 | 0 | default | no |
| `test_tool_discovery` | 6 | 0.6 | 0 | default | yes |
| `test_notification_cli` | 9 | 0.6 | 0 | default | no |
| `test_disk_combined` | 10 | 0.6 | 0 | default | no |
| `test_pane_close_round3` | 8 | 0.6 | 0 | default | no |
| `test_outside_land_scan` | 0 | 0.6 | 0 | integration only | no |
| `test_disk_proof_foreign` | 9 | 0.6 | 0 | default | no |
| `test_wsl_worker_argv` | 5 | 0.6 | 0 | default | no |
| `test_disk_removal` | 9 | 0.5 | 0 | both (4 integration) | no |
| `test_herdr_client` | 37 | 0.5 | 0 | default | no |
| `test_landing_repository` | 0 | 0.5 | 0 | integration only | yes |
| `test_runner` | 1 | 0.5 | 0 | both (10 integration) | no |
| `test_ci_entrypoint` | 0 | 0.5 | 0 | integration only | no |
| `test_pane_screen_walk` | 17 | 0.5 | 0 | default | no |
| `test_pane_launch_files` | 23 | 0.5 | 0 | default | no |
| `test_disk_elevated` | 10 | 0.5 | 0 | default | no |
| `test_disk_cpu_limit` | 11 | 0.5 | 0 | both (3 integration) | no |
| `test_config_sources` | 3 | 0.5 | 0 | default | no |
| `test_runner_status_write` | 7 | 0.5 | 0 | default | no |
| `test_disk_placement` | 4 | 0.5 | 0 | default | no |
| `test_outside_land_history` | 0 | 0.5 | 0 | integration only | no |
| `test_disk_yanked` | 0 | 0.5 | 0 | integration only | no |
| `test_disk_registration_timeouts` | 6 | 0.5 | 0 | default | no |
| `test_brief` | 3 | 0.5 | 0 | default | no |
| `test_disk_tracker` | 4 | 0.5 | 0 | default | no |
| `test_windows_load` | 6 | 0.5 | 0 | default | no |
| `test_disk_settled_deadline` | 3 | 0.5 | 0 | default | no |
| `test_disk_worker_status` | 3 | 0.5 | 0 | default | no |
| `test_vocabulary` | 2 | 0.5 | 0 | default | no |
| `test_pane_screen` | 50 | 0.4 | 0 | default | no |
| `test_disk_registrations` | 1 | 0.4 | 0 | both (13 integration) | no |
| `test_runner_run` | 0 | 0.4 | 0 | integration only | no |
| `test_runner_detach` | 0 | 0.4 | 0 | integration only | no |
| `test_pane_screen_footers` | 11 | 0.4 | 0 | default | no |
| `test_pane_status_hook` | 17 | 0.4 | 0 | default | no |
| `test_wsl_cache_reclaim` | 18 | 0.4 | 0 | default | no |
| `test_runner_environment` | 0 | 0.4 | 0 | integration only | no |
| `test_disk_local_policy` | 0 | 0.4 | 0 | integration only | no |
| `test_tiers` | 4 | 0.4 | 0 | default | no |
| `test_telemetry` | 2 | 0.4 | 0 | default | no |
| `test_landing_manual` | 0 | 0.4 | 0 | integration only | no |
| `test_rcw_paths` | 3 | 0.4 | 0 | default | no |
| `test_resources_scope` | 2 | 0.4 | 0 | default | no |
| `test_ci` | 0 | 0.4 | 0 | integration only | no |
| `test_pane_guard` | 25 | 0.3 | 0 | default | no |
| `test_disk_setup` | 1 | 0.3 | 0 | default | no |
| `test_parallel_runner` | 0 | 0.3 | 0 | integration only | no |
| `test_terminology` | 3 | 0.3 | 0 | default | no |

## Checkpoints

No checkpoint has run yet.

## After slice 0

Taken on 0b5de66 by the same `scripts/test.py -j 4 --durations 10`: `Ran 3901 tests (26 skipped) in 269 modules, wall 266.2s, 0 failed modules`. The integration tier: `Ran 209 tests (14 skipped) in 269 modules, wall 284.5s, 0 failed modules`. `test_disk_decision_trace` 14.5 s, `test_seams` 3.3 s. Available memory was short and varied, so these seconds compare loosely with the baseline. The census stopped at 199 of 269 modules for low memory, so this row has no spawn total: 197 of 198 modules match the baseline, and `test_garden_branch` read 279 against 280.
