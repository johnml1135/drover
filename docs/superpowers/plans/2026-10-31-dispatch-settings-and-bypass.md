# Dispatch settings, Bypass and landing detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each epic is Drover- or Director-dispatched, the owner can yank one task out of Drover, landings made outside Drover are detected, and the skill's capacity, conflict and monitor checks live in the hub.

**Architecture:** Epics gain a `dispatch` column and tasks a `picked_at`; `claim_next` is the one enforcement point, so every claimer obeys it. A new `yanked` task state closes a task without deciding whether it landed. Detection runs git proof, then the `decider`, then an Inbox question. Conflict holds and monitors run from the dispatcher tick, outside ledger transactions.

**Tech Stack:** Python 3.13, unittest, SQLite, FastAPI hub, git 2.51 (`merge-tree --write-tree`), local `decider`.

**Spec:** `docs/superpowers/specs/2026-10-31-drover-replaces-herdr-skill-design.md`, Phase 2. Decision record: ADR-0014.

## Global Constraints

- `dispatch_enabled`, `dispatch_repos` and STOP stay as they are, above the setting.
- Switching changes only who picks next; running work, checks, review and the merge queue carry on.
- Only the owner yanks. A yank stops the run, leaves files alone and writes one closing ledger entry.
- The merge queue is the only route to main in both settings.
- Decider is shadow first; live only when it cites a real commit on main.
- Parallel cap default 40, range unchanged (1 to 64).
- Git failure or unknown state: no landing recorded, no hold, keep going.
- Never run git inside a ledger transaction.
- No new dependency, no install, no push, no merge, no deploy.
- Tests: `DROVER_TESTS=integration python -m unittest <module>`. Hub suites need the hub venv: `H:\repos\drover.deploy\hub\.venv\Scripts\python.exe`, read-only.
- Commits are one declarative sentence and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- A branch with no commits of its own, or one sitting at the base: it is never a landing, though `contained()` alone says it is.
- A yank arriving during a gate, a review, a landing batch, or just before a hub restart: no `IllegalTransition`, no orphan run, no return to `ready`.
- A task in a Director-dispatched epic that fails and retries: its pick survives, and a switch back to Drover dispatch needs no cleanup.
- A branch that is deleted, or exists only in a WSL clone: detection and the conflict hold both treat it as unknown and stay quiet.
- The decider is down or names a commit that is not on main: one Inbox question for the task, never one per tick.

---

### Task 1: Each epic has a dispatch setting

**Files:**
- Modify: `drover/ledger/schema.py` (new `V18`, appended to `MIGRATIONS`), `drover/ledger/ops_execution.py:71-110`, `drover/ledger/ops.py`, `drover/ledger/ops_tasks.py`
- Create: `drover/ledger/ops_dispatch.py`
- Modify: `drover/dispatcher.py`, `hub/api.py`, `hub/store_ledger.py`, `hub/web.py`, `hub/templates/` (epic and task pages)
- Test: `tests/test_dispatch_setting.py` (new), `tests/test_hub_api.py`

**Interfaces:**
- Produces: `V18` adds `epics.dispatch TEXT NOT NULL DEFAULT 'drover' CHECK(dispatch IN ('drover','director'))` and `tasks.picked_at TEXT`, `tasks.picked_by TEXT`.
- Produces in `ops_dispatch`: `DROVER = 'drover'`, `DIRECTOR = 'director'`, `DEFAULT_KEY = 'dispatch_default'`; `mode_of(conn, task: dict) -> str`; `set_epic_mode(conn, epic_id, mode, actor='owner') -> dict`; `set_default_mode(conn, mode, actor='owner') -> str`; `pick(conn, task_id, actor) -> dict`; `awaiting_pick(conn) -> int`.
- Produces routes: `GET/POST /dispatch/default`, `POST /epics/{eid}/dispatch` (both owner only), `POST /tasks/{tid}/pick` (owner or API token).

- [ ] **Step 1:** Write tests: `test_default_is_drover`, `test_director_epic_task_is_not_claimed`, `test_picked_task_is_claimed`, `test_task_without_epic_follows_global_default`, `test_only_owner_switches`, `test_switch_leaves_claimed_and_running_alone`, `test_pick_survives_a_retry` (ready, claimed, failed, ready is still claimable), `test_pick_refused_unless_ready`, `test_dispatch_repos_still_binds_a_picked_task`, `test_stop_still_binds_a_picked_task`, `test_hub_claim_route_obeys_the_setting`, `test_ready_listing_still_shows_unpicked`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Add `V18` and `ops_dispatch`. `mode_of` reads the epic's column, or `dispatch_default` for a task with no epic.
- [ ] **Step 4:** In `claim_next`, after the `dispatch_repos` check, skip a candidate whose mode is `director` and whose `picked_at` is null. `ready_tasks` itself is unchanged, so listings still show the task.
- [ ] **Step 5:** Add the routes and store methods, an owner form on the epic page, and a Pick button on the task page. Report `awaiting-director` in the heartbeat `not_dispatched` from `awaiting_pick`.
- [ ] **Step 6:** Run `tests.test_dispatch_setting` and the hub API suite under the hub venv; expect OK. Commit.

### Task 2: A yanked task is closed but not yet decided

**Files:**
- Modify: `drover/ledger/states.py`, `drover/ledger/ops.py`, `drover/ledger/disk_notices.py:37`, `drover/ledger/waiting.py:34-39`, every `'cancelled'` literal site listed by `grep -rn "'cancelled'" drover hub`
- Create: `drover/ledger/ops_bypass.py`
- Modify: `hub/api.py`, `hub/store_ledger.py`, `hub/web.py`, `hub/templates/task.html`
- Modify: `docs/superpowers/specs/2026-10-31-drover-replaces-herdr-skill-design.md` (Bypass paragraph)
- Test: `tests/test_bypass.py` (new), `tests/test_ledger.py`

**Interfaces:**
- Produces: `states.YANKED = 'yanked'`, in `STATES`, not in `TERMINAL`, `ACTIVE` or `PENDING`. `TRANSITIONS[YANKED] = {LANDED, CANCELLED}`. Every non-terminal state gains `YANKED`. All of `(*, YANKED)` and `(YANKED, CANCELLED)` are in `OWNER_ONLY`. `(YANKED, LANDED)` is not: Task 5 records proved landings.
- Produces: `states.CLOSED = (*TERMINAL, YANKED)`: no run should exist and cleanup may act.
- Produces in `ops_bypass`: `yank(conn, task_id, actor='owner', reason='') -> dict`; `abandon(conn, task_id, actor='owner') -> dict`; `handback(conn, task_id, actor='owner') -> dict` returning the new task.
- Produces routes: `POST /tasks/{tid}/yank`, `/abandon`, `/handback`, all owner only.

- [ ] **Step 1:** Write tests: `test_yank_is_owner_only`, `test_yank_writes_exactly_one_bypass_event`, `test_yank_from_every_open_state`, `test_yank_of_landed_or_cancelled_refused`, `test_second_yank_is_idempotent_and_writes_nothing`, `test_yanked_is_never_claimed`, `test_dependent_stays_blocked_while_yanked`, `test_abandon_cancels_and_fires_the_terminal_path`, `test_handback_creates_a_draft_task_and_cancels_the_old_one`, `test_handback_retargets_blocks_deps_so_dependents_keep_waiting`, `test_handback_needs_owner_approval_to_run`, `test_handback_keeps_branch_and_worktree_fields_empty`, `test_yanked_scope_no_longer_blocks_new_work`, `test_snapshot_marks_yanked_terminal_and_not_live`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Change `states.py`, then implement `ops_bypass`. `yank` calls `set_status` and writes one `bypass` event with `from`, `reason` and the worktree path. It does not touch files.
- [ ] **Step 4:** `handback` is one transaction: copy title, goal, acceptance, scope, repo, repo_path, epic, priority, size and risk into a new `draft` task; point `task_deps.depends_on` at the new id; move the old task to `cancelled`; write a `handback` event on both tasks.
- [ ] **Step 5:** In `disk_notices.replay`, use `states.CLOSED` for `terminal` and for `live`, so a yanked worktree is reported terminal once nothing runs. Keep `waiting.py` on `TERMINAL`, so dependents wait for the answer. Review each `'cancelled'` literal and add `yanked` where it means "show or count as closed".
- [ ] **Step 6:** Add routes and a Yank, Abandon and Hand back control on the task page. In the spec's Bypass paragraph add: "A yanked task stays closed in a `yanked` state until it is detected as landed or the owner abandons it, because tasks that depend on it need that answer."
- [ ] **Step 7:** Run `tests.test_bypass tests.test_ledger tests.test_cleanup_settled`, using whichever suites cover `disk_notices`; expect OK. Commit.

### Task 3: A yank stops the run wherever it is

**Files:**
- Modify: `drover/dispatcher.py`, `drover/dispatcher_admission.py`, `drover/dispatcher_recovery.py`, `drover/dispatcher_gates.py`, `drover/dispatcher_reviews.py`, `drover/landing.py`, `drover/port.py`
- Test: `tests/test_bypass_runs.py` (new)

**Interfaces:**
- Consumes: `states.YANKED`, `ops_bypass.yank`.
- Produces: `Dispatcher._stop_yanked() -> list[int]`, called each tick before the control checks, including while dispatch is paused. Run outcome string `'yanked'`.

- [ ] **Step 1:** Write tests with a recording launcher: `test_tick_cancels_the_run_of_a_yanked_task`, `test_paused_dispatch_still_stops_it`, `test_worker_exit_after_yank_ends_the_run_and_leaves_status`, `test_gate_finish_after_yank_leaves_status`, `test_review_finish_after_yank_leaves_status`, `test_restart_after_yank_kills_the_orphan_and_does_not_requeue`, `test_landing_batch_drops_a_task_yanked_before_it_forms`, `test_batch_in_flight_when_yanked_lands_or_refuses_without_raising`, `test_seat_slot_is_released`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `_stop_yanked`: for each open run of a yanked task, call `launcher.cancel(run_id, nonce)` and track it in `self.cancelling`, as `_stop_for_control` does. Retries are covered by the existing scan.
- [ ] **Step 4:** Every path that moves a task after a run ends reads the current status first; when it is `yanked`, end the run with outcome `yanked` and move nothing. Put the check in one helper on the dispatcher, not at each `set_status`.
- [ ] **Step 5:** Landing batch formation already takes `reviewed` tasks; re-read status just before the batch lands, and drop a yanked task from it. If it has already reached main, `yanked -> landed` is legal, so record the landing.
- [ ] **Step 6:** Run `tests.test_bypass_runs` and the dispatcher, pipeline and landing suites; expect OK. Commit.

### Task 4: Cleanup applies the settled rule to yanked worktrees

**Files:**
- Modify: `drover/disk_worktrees.py:109-130,180-200`, `drover/disk_cleanup_decisions.py` only if a test requires it
- Test: `tests/test_disk_yanked.py` (new)

**Interfaces:**
- Consumes: the `terminal`/`live` snapshot from Task 2.

- [ ] **Step 1:** Write tests against real temporary worktrees: `test_builds_of_a_yanked_worktree_purge_on_the_idle_rule`, `test_committed_idle_three_days_removes_worktree_keeps_branch`, `test_dirty_yanked_worktree_is_kept`, `test_unpushed_commits_alone_do_not_block_removal_because_branch_is_kept`, `test_running_process_keeps_it`, `test_idle_two_days_is_kept`, `test_unmerged_yanked_worktree_needs_no_approval`.
- [ ] **Step 2:** Run them and confirm which fail.
- [ ] **Step 3:** Make `settled` treat a closed task with no merged proof as removable when all committed, nothing running, 3 days idle. It must never delete the branch. Passing tests with no code change are fine; report that.
- [ ] **Step 4:** Run `tests.test_disk_yanked` and all `tests.test_disk*`; expect OK apart from the existing `fastapi.sse` error under system Python. Commit.

### Task 5: Git proves outside landings, and the owner can clear a hold

**Files:**
- Create: `drover/outside_land.py`
- Modify: `drover/ledger/ops_outside.py`, `drover/landing.py`, `drover/dispatcher.py`, `hub/web.py`, `hub/store_ledger.py`, `hub/templates/landing.html`
- Test: `tests/test_outside_land.py` (new), `tests/test_landing.py`

**Interfaces:**
- Salvage from `feat/manual-land` (`git show feat/manual-land:drover/landing.py`, lines 310-352): `landing.contained(repo, branch, main_sha) -> bool`, `landing.verify_manual_land(repo, branch, main_sha, default_branch) -> str`, `landing.clear_hold(conn) -> dict`. Drop `record_manual_land` and its form.
- Produces in `ops_outside`: `record_detected_landing(conn, task_id, sha, base_ref, proof, actor='detector') -> dict`, from `yanked` or `needs-owner`, sharing its body with `record_external_landing`. It requires the same SHA and ancestor checks, and writes `external_landing` with `gate='outside-drover'`, `review=proof`.
- Produces in `outside_land`: `git_proof(repo, branch, base_ref) -> dict | None`, keys `sha`, `proof` (`'ancestor' | 'patch-equivalent' | 'adds-nothing'`); `candidates(conn) -> list[dict]`; `tick(conn, cfg, *, decide=None) -> dict`; `OWN_COMMITS` check below.

- [ ] **Step 1:** Write tests on real temporary repos: `test_fast_forward_is_ancestor`, `test_rebased_is_patch_equivalent`, `test_squash_merge_adds_nothing`, `test_partial_squash_is_not_proved`, `test_branch_at_base_is_not_a_landing`, `test_branch_with_no_own_commits_after_rebase_needs_task_evidence`, `test_missing_branch_is_unknown`, `test_git_failure_is_unknown`, `test_yanked_task_lands_and_unblocks_dependents`, `test_needs_owner_task_lands`, `test_running_task_is_not_scanned`, `test_scan_uses_remote_ref_when_push_is_on`, `test_second_tick_does_nothing`, `test_clear_hold_is_owner_only_and_answers_retry`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Port the three functions. `git_proof` needs positive evidence that the task produced commits: the branch has commits of its own relative to the merge base with its recorded first run, or the ledger holds a run with a commit SHA that is on the base. A branch that never diverged returns `None`. "Adds nothing" is `git merge-tree --write-tree <base> <branch>` equal to `<base>^{tree}`.
- [ ] **Step 4:** Implement `candidates` (write tasks with a branch in `yanked` or `needs-owner`) and `tick`, reading the ledger, running git outside any transaction, then recording in one short transaction. Record each proved landing as a `status` event, a `task_landed` row and released holds, the rows a real landing writes.
- [ ] **Step 5:** Call `tick` from the dispatcher at most once per `watch_interval_s` (default 600, Task 8), and catch and log all exceptions so a detection failure never stops dispatch.
- [ ] **Step 6:** Add an owner-only `POST /landing/clear-hold` form in `hub/web.py` with a button on the landing page, as in `feat/manual-land`.
- [ ] **Step 7:** Run `tests.test_outside_land tests.test_landing` and the ledger suite; expect OK. Commit.

### Task 6: The decider, then the Inbox, for what git cannot prove

**Files:**
- Modify: `decide-criteria.json`, `drover/outside_land.py`, `drover/ledger/disk_notices.py`
- Test: `tests/test_outside_land_decider.py` (new), `tests/test_decide.py`

**Interfaces:**
- Produces: decide question `landed_outside`, `type: choice`, `dynamic: true`, `mode: shadow`. Dynamic criteria are one option per candidate commit, keyed by full SHA, plus `not_landed`.
- Produces: `outside_land.ambiguous(repo, task, base_ref) -> list[str]`: up to 20 commits on the base since the branch's merge base whose subject or changed files overlap the task. Empty means nothing to ask.
- Produces: `disk_notices.publish(conn, notice, *, kind='disk.notice', epic_title='Disk space', actor='disk') -> int`, the existing function generalised; `retract` takes the same `kind`. `outside_land` uses `kind='landing.notice'`.

- [ ] **Step 1:** Write tests with a fake `decide`: `test_shadow_answer_is_recorded_and_changes_nothing`, `test_live_answer_citing_a_commit_on_main_lands`, `test_live_answer_citing_an_unknown_sha_is_ignored`, `test_live_answer_for_a_commit_off_main_is_ignored`, `test_live_not_landed_asks_the_owner`, `test_decider_down_asks_the_owner`, `test_low_confidence_asks_the_owner`, `test_no_candidates_asks_nothing_for_needs_owner_tasks`, `test_one_question_per_task_across_ticks`, `test_owner_answer_landed_records_the_named_commit`, `test_owner_answer_not_landed_abandons_a_yanked_task`, `test_question_retracts_when_git_later_proves_it`. Add `test_landed_outside_defaults_to_shadow` to `tests.test_decide`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Add the question to `decide-criteria.json`, with criteria that tell the model to cite a commit only if it carries all of the task's accepted work. Do not raise `criteria_version` on other questions.
- [ ] **Step 4:** Extend `tick`: no git proof and at least one candidate commit calls `decide`; `acted` is honoured only after `git_proof`-style verification that the SHA is on the base; every other outcome publishes one Inbox question with options `Landed at <sha>` per candidate, `Not landed`, and free text. A yanked task with no candidates stays quiet: Drover does not watch it.
- [ ] **Step 5:** Consume answers in `tick`: `Landed at` calls `record_external_landing` as owner, moving a yanked task first through the shared body; `Not landed` calls `ops_bypass.abandon` for a yanked task and leaves a `needs-owner` task as it is.
- [ ] **Step 6:** Run `tests.test_outside_land_decider tests.test_decide tests.test_outside_land`; expect OK. Commit.

### Task 7: The cap is the owner's, and likely collisions wait

**Files:**
- Modify: `drover/config_defaults.py:83`, `drover/port.py:172-178`, `drover/tuner.py:35,51,179,257-261`, `drover/dispatcher.py`
- Create: `drover/dispatch_hold.py`
- Modify: `docs/configuration.md`, `drover.example.toml`
- Test: `tests/test_dispatch_hold.py` (new), `tests/test_tuner.py`

**Interfaces:**
- Produces: `capacity.parallel_cap` default 40. `port.get_policy()['parallel_cap']` always reads config; stored policy values are ignored, and the tuner no longer proposes it.
- Produces config: `[dispatch] magnets`, default the skill's `DEFAULT_MAGNETS` (`H:\repos\my-skills\herdr\wsl\admit-slice.py:50`), and `[dispatch] conflict_hold = true`.
- Produces in `dispatch_hold`: `changed_files(repo, branch, base_ref, cache) -> set[str] | None`; `reason(candidate_scope, other_files, patterns) -> str | None`; `holds(ready: list[dict], active: list[dict], cfg, git=...) -> dict[str, str]`, mapping task id to a reason naming the other task and file.

- [ ] **Step 1:** Write tests: `test_cap_default_is_40`, `test_stored_policy_cap_6_is_ignored`, `test_tuner_leaves_the_cap_alone`, `test_shared_file_holds_behind_active_branch`, `test_same_magnet_pattern_holds_on_different_files`, `test_no_overlap_starts`, `test_empty_scope_is_not_held`, `test_other_repo_never_holds`, `test_unknown_branch_holds_nothing`, `test_hold_clears_when_the_branch_lands`, `test_held_task_stays_ready_and_is_reported_as_conflict_hold`, `test_picked_task_is_held_too`, `test_git_runs_once_per_branch_tip_across_ticks`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Change the cap. Remove `parallel_cap` from `AUTO_KEYS` and drop `tune_cap`'s use, keeping the rest of the tuner.
- [ ] **Step 4:** Implement `dispatch_hold`. `other_files` is `git diff --name-only <merge-base>..<branch>` for each `states.ACTIVE` task in the same repo, cached by tip SHA. Patterns use `fnmatch`, as the skill does.
- [ ] **Step 5:** In `tick`, compute `holds` before the claim loop, outside any transaction, add the ids to `skip`, and count them as `conflict-hold` in `skipped`. A failure logs and holds nothing.
- [ ] **Step 6:** Run `tests.test_dispatch_hold tests.test_tuner` and the config and dispatcher suites; fix fixtures that assert a cap of 4. Commit.

### Task 8: Warden, velocity and context-watch become hub trackers

**Files:**
- Create: `drover/watch.py`
- Modify: `drover/dispatcher.py`, `drover/config_defaults.py`, `drover/config_validation.py`, `docs/configuration.md`
- Test: `tests/test_watch.py` (new)

**Interfaces:**
- Consumes: `disk_notices.publish/retract` with `kind='watch.notice'`, `landing_batch.velocity`, `resources` verdict, `launch_usage` context readings.
- Produces config: `[watch] interval_s = 600`, `stalled_hours = 24`, `context_tokens = 300000`, `red_rate = 0.5`, `min_gates = 6`.
- Produces: `watch.tick(conn, cfg, resources: dict, now) -> list[str]`, the notice ids open after the tick; pure check functions `warden(resources)`, `velocity(conn, cfg, now)`, `context(runs, cfg)`, each returning `[{'id', 'title', 'body'}]`.

- [ ] **Step 1:** Write tests: `test_hold_verdict_opens_a_warden_notice`, `test_clear_verdict_retracts_it`, `test_reviewed_task_stalled_24h_opens_velocity_notice`, `test_red_rate_over_half_needs_six_gates`, `test_run_over_300k_opens_context_notice`, `test_a_notice_is_published_once`, `test_interval_is_honoured`, `test_failure_in_one_check_leaves_the_others`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `watch.py`. Read `H:\repos\my-skills\herdr\references\warden.md` and `references\velocity-and-churn.md` for what each check reports, and name what you did not port. Context reads the source `rotation_handoff` uses; no reading means no notice.
- [ ] **Step 4:** Call `watch.tick` and `outside_land.tick` from the dispatcher on the `interval_s` clock, each in its own `try`, whether or not dispatch is paused.
- [ ] **Step 5:** Run `tests.test_watch` and the dispatcher suites; expect OK. Commit.

### Task 9: Docs and whole-branch verification

**Files:**
- Modify: `docs/configuration.md`, `README.md`, `docs/operations/disk-budget-rollout.md` or a new `docs/operations/dispatch-rollout.md`, `GLOSSARY.md`
- Test: existing suites

- [ ] **Step 1:** Document the settings, routes, `yanked`, handback and the decider's shadow step. Keep implementation out of the glossary; add only terms a reader needs, such as Yanked task and Handback.
- [ ] **Step 2:** Run every `tests.test_*` module on Windows with `DROVER_TESTS=integration`, hub suites under the hub venv. Expect OK; report any existing failure by name with its base-commit result.
- [ ] **Step 3:** Run `tests.test_dispatch_setting tests.test_bypass tests.test_outside_land tests.test_dispatch_hold` under WSL on ext4 in a temporary clone under `~/work`.
- [ ] **Step 4:** Migrate a copy of the live ledger (`C:\Users\johnm\.drover`) in the scratchpad, never the original, and confirm V18 applies and tasks still list.
- [ ] **Step 5:** Commit. Report owner steps: deploy the hub, then watch the shadow answers before setting `landed_outside` live.
