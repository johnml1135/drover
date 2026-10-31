# Disk budget and overnight compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drover keeps WSL used space under budget unattended, and compacts the VHDX overnight.

**Architecture:** A new WSL-used reading feeds an independent trigger in `disk.tick`. A budget stage purges disposable paths least recently used first. Cleanup stops consulting load and pins itself to 4 CPUs. Compaction is a Windows-only module driven by an elevated scheduled task.

**Tech Stack:** Python 3.13, unittest, git, PowerShell Hyper-V cmdlets.

**Spec:** `docs/superpowers/specs/2026-10-31-drover-replaces-herdr-skill-design.md`, Phase 1.

## Global Constraints

- WSL used thresholds: purge at 300 GB, purge target 225 GB, stop new work at 500 GB. All configurable under `[disk_cleanup]` as `wsl_purge_gb`, `wsl_target_gb`, `wsl_stop_gb`.
- CPU cap default 4, `[disk_cleanup] cpu_limit`.
- Unattended apply deletes disposable paths only.
- Unknown process state means keep. The cleanup task is not elevated.
- `sparseVhd` stays off. Never install packages. No push.
- Tests: `DROVER_TESTS=integration python -m unittest <module>`; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Pane-based stop messages belong to Phase 3. This plan stops headless workers and kills processes.

## Review Focus

- WSL is stopped when the reading is due: the reading stays `unknown`, starts nothing, and never boots WSL for a reading.
- A worktree in use by an open editor or build: its paths are kept even when the purge target is unmet.
- The target cannot be reached: the purge ends, reports the shortfall, and does not loop or spill into non-disposable paths.
- Compaction fails midway, for example `Optimize-VHD` returns an error or the VHD stays mounted: dismount, restart WSL, release the hold, report.
- The hub or `HerdrDiskGuard` re-wakes WSL during the window: the job detects a running distro before mounting and stops rather than failing mid-optimize.

---

### Task 1: Cleanup ignores load and runs on a CPU cap

**Files:**
- Modify: `drover/disk_cleanup.py:132-190`, `drover/disk.py:243-255`, `drover/placement_worker.py:170-190`, `drover/config_defaults.py`
- Modify: `docs/configuration.md`, `drover.example.toml`
- Test: `tests/test_cleanup_throttle.py`, `tests/test_disk_cpu_limit.py` (new)

**Interfaces:**
- Produces: `placement_worker.limit_cpus(count: int, *, windows=None, set_mask=None, available=None) -> int`, returning the number of CPUs in effect.

- [ ] **Step 1:** Replace `test_hold_before_a_stage_aborts_cleanly` with `test_hold_before_a_stage_does_not_abort`: a `held()` returning reasons leaves `result` without `aborted` and all stages run.
- [ ] **Step 2:** Add `test_tick_launches_under_load`: with `gate()` returning `{'verdict': 'hold', ...}`, `disk.tick` still enqueues and records no `cleanup_deferred`.
- [ ] **Step 3:** Add `test_limit_cpus_masks_first_four_of_eight` (mask `0b1111`), `test_limit_cpus_never_exceeds_available` (2 of 2) and `test_limit_cpus_wsl_uses_sched_setaffinity`.
- [ ] **Step 4:** Run them and confirm they fail.
- [ ] **Step 5:** Remove the `held` and `gate` load paths; `aborted_load` no longer occurs. Keep lock, cooldown and critical behaviour.
- [ ] **Step 6:** Implement `limit_cpus` with `SetProcessAffinityMask` on Windows and `os.sched_setaffinity` elsewhere, best effort like `lower_priority`, and call it beside `lower_priority` with `disk_cleanup.cpu_limit` (default 4, validated positive int).
- [ ] **Step 7:** Run `tests.test_cleanup_throttle tests.test_disk_cpu_limit` plus `tests.test_placement`; expect OK. Commit.

### Task 2: WSL used space is read and triggers on its own

**Files:**
- Modify: `drover/placement_host.py:148-170`, `drover/disk.py:30-60,168-260`, `drover/disk_cleanup.py:115-160`, `drover/config_defaults.py`, `drover/config_validation.py`
- Modify: `GLOSSARY.md` (Tracker, Disk tracker)
- Test: `tests/test_disk_wsl_used.py` (new)

**Interfaces:**
- Produces: `placement_host.wsl_used_gb(distro, up=True, *, run=subprocess.run, ttl=WSL_FREE_TTL_S) -> float | None`.
- Produces: `disk.wsl_budget(cfg, *, used=None) -> dict` with keys `used_gb`, `level` (`'ok' | 'purge' | 'stop' | 'unknown'`), `target_gb`.

- [ ] **Step 1:** Add tests: `test_used_reads_df_used_column`, `test_not_up_returns_none_and_runs_nothing`, `test_levels_at_299_300_499_500` (`ok`, `purge`, `purge`, `stop`), `test_h_ok_but_wsl_over_triggers`, `test_wsl_ok_but_h_low_triggers`, `test_unknown_reading_triggers_nothing`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `wsl_used_gb` with `df -B1 --output=used /`, sharing the cache and guard rules of `wsl_free_gb`.
- [ ] **Step 4:** Implement `disk.wsl_budget`; `tick` claims a cleanup episode named `wsl` at level `purge` or `stop`, with its own cooldown state, independent of drive episodes.
- [ ] **Step 5:** Run the tests; expect OK. Update the glossary so a Tracker may also measure used space inside WSL. Commit.

### Task 3: Budget purge, least recently used first

**Files:**
- Create: `drover/disk_budget.py`
- Modify: `drover/disk_paths.py:327-420`, `drover/disk_side.py`, `drover/disk_cleanup.py:150-215`
- Test: `tests/test_disk_budget.py` (new)

**Interfaces:**
- Consumes: `disk.wsl_budget`, `disk_paths.output_evidence`.
- Produces: `disk_budget.plan(entries: list[dict], need_bytes: int) -> list[dict]`, newest-idle last; `disk_budget.need_bytes(used_gb, target_gb) -> int`.

- [ ] **Step 1:** Add tests: `test_oldest_idle_goes_first`, `test_stops_once_target_met`, `test_in_use_is_skipped_even_when_short`, `test_idle_under_two_hours_is_kept`, `test_shortfall_is_reported_with_reason_budget_shortfall`, `test_never_lists_non_disposable_paths`, `test_dry_run_changes_nothing`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `plan` as a pure function over evidence rows sorted by idle age; the WSL side's output stage uses it when the request carries `budget=dict(target_gb=...)`, re-reading used space after each removal batch.
- [ ] **Step 4:** Run `tests.test_disk_budget` and all `tests.test_disk*` modules; expect OK apart from the existing `fastapi.sse` error. Commit.

### Task 4: 500 GB stops new work

**Files:**
- Modify: `drover/disk.py:23-30,81-97`, `drover/dispatcher_admission.py`
- Test: `tests/test_disk_wsl_used.py`

**Interfaces:**
- Consumes: `disk.wsl_budget`.

- [ ] **Step 1:** Add `test_stop_level_holds_new_wsl_work`, `test_stop_level_leaves_running_work`, `test_windows_work_still_admitted_at_stop`, `test_unknown_used_holds_nothing`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Make `disk.held('wsl', cfg)` true at level `stop`, with a reason naming the used and limit values.
- [ ] **Step 4:** Run the tests; expect OK. Commit.

### Task 5: The machine repo has a local disposable policy

**Files:**
- Modify: `drover/repos.py:92,240-275`, `drover/repos_normalize.py:46`, `drover/disk_worktrees.py:66-75`, `drover/config.py:366`
- Test: `tests/test_disk_local_policy.py` (new)

**Interfaces:**
- Produces: repo key `local_disposable: list[str]` in `~/.drover/repos/<name>.toml`.

- [ ] **Step 1:** Add `test_local_disposable_used_when_repo_has_no_committed_config`, `test_local_policy_is_validated_like_committed`, `test_local_policy_survives_repo_save`, `test_committed_and_local_lists_merge_without_duplicates`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement. `outputs_at` returns the committed list plus `local_disposable`; `_adopted` is not required for the local list.
- [ ] **Step 4:** Run the tests; expect OK. Commit.

### Task 6: Overnight compaction job

**Files:**
- Create: `drover/compaction.py`, `drover/compaction_vhd.py`, `scripts/Install-CompactionTask.ps1`
- Modify: `drover/dispatcher.py`, `drover/placement_host.py`, `drover/config_defaults.py`, `docs/configuration.md`
- Test: `tests/test_compaction.py` (new)

**Interfaces:**
- Consumes: the dispatcher's durable stop controls and the hold from Task 4.
- Produces: `compaction.run(cfg, *, runtime=None, now=None) -> dict` with keys `status` (`'compacted' | 'skipped' | 'failed'`), `stopped`, `killed`, `requeued`, `before_gb`, `after_gb`, `errors`. `compaction_vhd.compact(vhdx: str, *, run=subprocess.run) -> dict`. Setting `compaction_active` in the ledger; while true, `wsl_free_gb` and `wsl_used_gb` return their cached value without calling `wsl.exe`.

- [ ] **Step 1:** Add `test_order_is_hold_stop_kill_trim_shutdown_compact_restart_release` using a recording runtime; `test_headless_gets_300s_then_killed_and_requeued`; `test_kill_covers_unaccounted_processes_by_pid`; `test_optimize_failure_dismounts_restarts_releases_reports`; `test_running_distro_before_mount_fails_safely`; `test_polling_makes_no_wsl_call_during_window`; `test_second_run_while_active_is_skipped`; `test_vhd_is_mounted_read_only`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `compact` from the working procedure: `Mount-VHD -ReadOnly`, `Optimize-VHD -Mode Full`, `Dismount-VHD`, always dismounting in `finally`. Find the VHDX from the distro's registry entry, never a hard-coded path.
- [ ] **Step 4:** Implement `run` with a stored record per step, so a crash leaves the hold releasable on the next hub start. Add a hook `pane_stop(runtime)` that returns nothing yet; Phase 3 fills it.
- [ ] **Step 5:** Run `tests.test_compaction`; expect OK.
- [ ] **Step 6:** Write `Install-CompactionTask.ps1`, registering `\DroverCompaction` nightly at 03:30 with `-RunLevel Highest`. Do not run it. Commit.

### Task 7: Rollout docs and whole-branch verification

**Files:**
- Modify: `docs/configuration.md`, `README.md`, `docs/operations/`
- Test: existing suites

- [ ] **Step 1:** Replace the "VHDX compaction remains a separate manual owner operation" text and the 7-day guard rule with the spec's 3 clean applied days.
- [ ] **Step 2:** Run the disk, cleanup, placement, pipeline, landing, config and architecture modules with `DROVER_TESTS=integration`; expect OK apart from the existing `fastapi.sse` error.
- [ ] **Step 3:** Run `tests.test_disk_budget tests.test_disk_wsl_used` under WSL on ext4 in a temporary clone under `~/work`.
- [ ] **Step 4:** Commit. Report the owner steps: deploy the hub, install applied schedules, run `Install-CompactionTask.ps1` elevated.
