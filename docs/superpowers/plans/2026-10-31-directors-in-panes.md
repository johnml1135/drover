# Directors in panes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One interactive Director, on any configured seat, runs in a Windows Herdr pane under Drover's pane safety rules, and Drover keeps working with Herdr closed.

**Architecture:** The hub is the only process that talks to Herdr, through one allowlisted client, and records every agent it starts in the ledger. A resident director is a `DirectorRuntime` run with a new adapter: wakes, session handoffs and the compaction stop all pass through one guarded write path, and any uncertainty leaves durable mail instead of input. The elevated compaction job still never opens the ledger: the hub runs the stop and the resume, and answers the job in a file.

**Tech Stack:** Python 3.13, unittest, SQLite, FastAPI hub, `herdr.exe` as a subprocess, WSL interop, Claude Code and Codex CLI hooks.

**Spec:** `docs/superpowers/specs/2026-10-31-drover-replaces-herdr-skill-design.md`, Phase 3, Skill retirement, Open issue. Decision record: ADR-0014, with ADR-0012 for the finite-execution default. Rule sources: `H:\repos\my-skills\herdr`, read-only.

**Base:** rebase `feat/directors-in-panes` onto `feat/dispatch-settings-bypass` after it lands. This plan uses Phase 2's `V18`, `disk_notices.publish(..., kind=...)`, `[watch] context_tokens` and `watch.context()`. If Phase 2 used more migrations, Task 5's number moves up.

## Global Constraints

- One Director role, one interactive director, multi-director parked (ADR-0012). A fresh agent must be able to replace it from the repo, ledger and brief.
- Any configured seat may be Director; Opus 5.5 is the default.
- Directors and Drover management run in Windows Herdr. Drover must work with Herdr closed.
- The adapter writes only to Drover's own idle, unfocused panes; other panes get durable mail. The compaction stop is the single exception, for Drover-started agents in WSL panes.
- Compaction stop: "stop now" up to 5 minutes, a rude interruption 1 minute, then kill. Resume with "restart complete, keep going". A failed resume starts a fresh agent and is flagged. Seat and placement are always recorded.
- The Codex "Trust this folder?" standing rule covers worktrees of already trusted repos only; every other dialog stays owner-only.
- Handoff file at 275k tokens (configurable), rotate at the next idle moment, forced at 300k. Drover sends no `/compact`; the owner may.
- Fresh agents load only short files matching their task through ADR `applies_to`, under a hard size limit, with no general overview.
- Wakes are grouped to at most one per window, default 10 minutes; urgent events use a short configurable window; the default wake limit is 6 per hour.
- Codex on Windows stays forbidden (ADR 0002). A Codex Director is a WSL process in a Windows pane, so compaction stops it.
- Drover's glossary wins: "lane", "gate", "receipt", "READY", "LAND", "HOLD" and "garden" retire from visible text; panes stay panes.
- No new dependency, no install, no push, no merge, no deploy. `H:\repos\my-skills` is not edited by any code task.
- Tests never reach a real Herdr server: they use the fake in `tests/fake_herdr.py` and the dummy socket from Task 5.
- Tests: `DROVER_TESTS=integration python -m unittest <module>`. Hub suites need the hub venv: `H:\repos\drover.deploy\hub\.venv\Scripts\python.exe`, read-only.
- Commits are one declarative sentence and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- Herdr closed or restarted mid-run, including a changed pane or terminal id: one notice, no exception out of the tick or the job, dispatch unaffected, and a pane whose identity changed is no longer Drover's. Test in Task 10.
- The owner focuses a pane or types between the final read and the send, or between paste and Enter on a WSL agent: no Enter, and the text is never resent. Test in Task 7.
- A context reading is unavailable (no transcript, no footer, WSL held): unknown causes neither a handoff request nor a rotation. Test in Task 12.
- A Codex Director's resume fails after compaction: the fresh agent starts after the hold is released and the morning report flags it with seat and placement. Test in Task 13.
- An event burst, an urgent event and the 6-per-hour cap arrive together: at most the allowed wakes, one Inbox question, and the wake stays durably pending. Test in Task 4.

## Waves

Tasks in one wave share no file and no unfinished interface, so they may run in parallel. A wave starts when the one before it is merged into this branch.

| Wave | Tasks | Why this order |
|---|---|---|
| 1 | 1, 2, 3, 4 | Seat rules and config keys, pure screen rules, the status hook, wake cadence: disjoint files |
| 2 | 5, 6 | Herdr client and registry; `applies_to` loading. Both read Task 1's keys |
| 3 | 7, 8 | Write path and pane close. Both use Tasks 2 and 5 |
| 4 | 9 | Launch uses Task 7 |
| 5 | 10 | The adapter uses Task 9. It creates `pane_wake.py` and `pane_handoff.py` as empty hooks, as `pane_stop` was in Phase 1 |
| 6 | 11, 12, 13 | Each fills only its own module after Task 10 |
| 7 | 14 | Glossary and docs; the only task that touches `GLOSSARY.md` and `docs/configuration.md` |
| 8 | 15 | Rule coverage and whole-branch verification |
| 9 | 16 | Skill retirement. Runs only after the owner confirms |

## The 32 pane safety rules

Sources are in `H:\repos\my-skills\herdr`. "Port of" names the skill test carried over; "new" means the skill never tested it. Tests are in `tests/test_<module>.py`. No Director runs in a pane until all 32 pass (Task 15).

| # | Rule | Source | Drover test | Port of | Task |
|---|---|---|---|---|---|
| 1 | Check Herdr is the intended target before controlling it. The hub runs outside Herdr, so this becomes: disabled or unresolved means no call | `SKILL.md:39` | `herdr_client.test_disabled_or_missing_binary_makes_no_call` | new | 5 |
| 2 | Never prompt, key, stop or close another's pane | `SKILL.md:40`, `references/dispatch.md:131-132,136-138` | `pane_send.test_pane_without_a_drover_row_gets_mail_not_input`, `pane_close.test_other_panes_are_never_closed` | `test_explicit_owner_reuse_preserves_foreign_and_controller_panes`, `test_focused_control_keep_warden_director_and_lead_panes_are_never_closed` | 7, 8 |
| 3 | Never overwrite owner input; a collapsed paste proves no ownership | `SKILL.md:41-43`, `references/supervision.md:169-171` | `pane_send.test_busy_blocked_unknown_and_draft_screens_send_nothing`, `pane_send.test_owner_draft_on_last_read_sends_nothing`, `pane_screen.test_collapsed_paste_is_pending_not_ours` | `test_busy_blocked_unknown_drafts_and_approval_ui_preserved`, `test_owner_draft_appearing_on_last_read_is_preserved`, `test_collapsed_long_paste_counts_as_pending` | 2, 7 |
| 4 | Scroll and copy viewports hold input; missing scroll metadata blocks; never send keys to clear them | `SKILL.md:43`, `references/supervision.md:24-27` | `pane_screen.test_copy_viewport_is_held_not_working`, `pane_screen.test_viewport_marker_in_transcript_is_not_held`, `pane_send.test_scrolled_or_unknown_viewport_sends_nothing`, `pane_send.test_viewport_is_checked_again_just_before_send` | `test_historical_copy_viewport_is_unknown_not_working_or_ready`, `test_viewport_marker_in_transcript_is_not_an_active_hold`, `test_native_host_scroll_blocks_even_with_a_normal_empty_composer`, `test_unavailable_or_invalid_native_scroll_metadata_fails_closed`, `test_malformed_native_metadata_fails_closed`, `test_native_scroll_metadata_is_checked_again_immediately_before_submission` | 2, 7 |
| 5 | Scheduling is not delivery: a wake counts only with a new increment, turn activity and a new token | `SKILL.md:43-46`, `references/supervision.md:15-23` | `pane_wake.test_wake_counts_only_with_turn_activity_and_a_new_checkpoint`, `pane_wake.test_unverified_wake_is_not_replayed_after_hub_restart` | `test_unverified_attempt_is_not_replayed_after_runner_recreation`, `test_detached_singleton_timer_wakes_only_its_ready_fixture_by_effect` | 11 |
| 6 | A stall does not prove non-delivery: read before resending, submit once, activity before live | `references/dispatch.md:80-85,135`, `references/recovery.md:44-48` | `pane_send.test_stalled_send_is_read_not_resent`, `pane_launch.test_brief_is_submitted_once_and_never_retried`, `pane_launch.test_empty_composer_after_send_proves_nothing`, `pane_launch.test_failed_submission_is_not_live` | `test_after_submission_dialog_or_pending_composer_never_retries`, `test_ready_needs_first_turn_activity_after_an_idle_composer`, `test_failed_submission_never_prints_ready` | 7, 9 |
| 7 | A visible Working row overrides a stale idle status | `references/dispatch.md:82`, `references/failure-modes.md:11,13` | `pane_screen.test_visible_working_overrides_stale_idle`, `pane_screen.test_narrow_pane_working_row` | `test_visible_working_overrides_stale_idle`, `test_narrow_pane_working_line_and_truncated_footer` | 2 |
| 8 | A wake in the queue for this exact scope is queued, not lost; a queue idle 10 minutes is wedged | `references/supervision.md:166,209-213` | `pane_screen.test_queued_wake_is_recognised_only_for_its_scope`, `pane_wake.test_queue_idle_ten_minutes_is_wedged_and_sends_no_key` | `test_queued_wake_visible_above_composer_is_not_unverified_during_work`, `test_another_queued_prompt_or_opaque_paste_does_not_prove_our_wake_delivery`, `test_idle_lane_with_a_queue_that_never_drains_is_queued_wedged` | 2, 11 |
| 9 | Trust and approval need durable authorization; the standing rule answers Codex "Trust this folder?" for trusted repos only | `SKILL.md:47-51`, `references/supervision.md:160` | `pane_launch.test_codex_trust_is_answered_for_a_worktree_of_a_trusted_repo`, `pane_launch.test_codex_trust_is_left_for_an_untrusted_repo`, `pane_launch.test_trust_unreadable_config_is_owner_only`, `pane_launch.test_claude_trust_dialog_stays_owner_only` | new decision tests; `test_trust_dialog_is_blocked_not_ready_and_never_answered` for the refusal | 9 |
| 10 | Startup consent applies only to the launcher's own new pane | `references/dispatch.md:133-134` | `pane_guard.test_startup_keys_answers_only_codex_trust_in_a_fresh_pane`, `pane_launch.test_reused_pane_inherits_no_consent` | `test_powershell_startup_consent_is_fresh_only_and_guard_ignores_scrollback`, `test_fresh_startup_trust_is_blocked_without_keys` | 2, 9 |
| 11 | Model, approval, question and selection dialogs block every path; strong modals beat Working; a dialog after a paste stops the Enter | `references/supervision.md:194-207`, `wsl/ui_guard.py:9-14` | `pane_guard.test_model_dialog_blocks_without_a_choice`, `pane_guard.test_selection_approval_question_trust_and_numbered_options_block`, `pane_guard.test_strong_modal_overrides_working`, `pane_guard.test_selected_option_is_not_a_composer`, `pane_guard.test_words_in_working_scrollback_do_not_block`, `pane_send.test_dialog_after_paste_stops_the_enter` | the whole of `test_ui_guard.py`, `test_dialog_after_paste_exits_one_and_preserves_it` | 2, 7 |
| 12 | Read the Claude bottom region, not scrollback; unknown layouts fail closed | `references/supervision.md:192` | `pane_screen.test_claude_uses_bottom_region_not_scrollback`, `pane_screen.test_unknown_claude_layout_fails_closed` | `test_claude_helper_uses_bottom_region_instead_of_scrollback_words` | 2 |
| 13 | Send only with ownership, unfocused, idle, empty composer, unchanged occupant; no raw fallback, no option to clear a dialog | `references/supervision.md:186-192`, `wsl/send-prompt.py:37-54` | `pane_send.test_requires_row_unfocused_idle_empty_and_unchanged`, `pane_send.test_occupant_change_between_reads_sends_nothing`, `pane_send.test_no_option_clears_a_dialog` | `test_say_helper_requires_ownership_and_preserves_model_dialog_and_drafts` | 7 |
| 14 | WSL agents: one bracketed paste, then Enter, behind every guard | `Send-AgentPrompt.ps1:6-10,46-56` | `pane_send.test_wsl_agent_gets_one_bracketed_paste_then_enter` | `test_powershell_send_helper_refuses_text_and_keys_before_any_mutation` | 7 |
| 15 | A wake never approves, changes models, restarts or prompts another's agent | `references/supervision.md:28` | `pane_wake.test_wake_text_has_no_digit_and_grants_nothing` | new | 11 |
| 16 | Prefer durable mail; storing mail never pastes; acknowledge only after a read | `references/supervision.md:188,197-198` | `pane_wake.test_wake_carries_no_message_body`, `pane_wake.test_mail_stays_pending_until_the_director_commits` | `test_coord.py` delivery cases | 11 |
| 17 | Composer text that does not match the recorded digest is an owner draft; Enter once only after verifying | `references/supervision.md:156,169-172` | `pane_screen.test_own_prompt_needs_the_full_digest`, `pane_screen.test_prefix_of_our_prompt_is_a_draft`, `pane_send.test_unsent_own_prompt_gets_one_enter` | `test_unsent_prompt_only_when_it_matches_the_last_sent_prompt`, `test_prefix_of_our_prompt_does_not_prove_owner_input_is_ours` | 2, 7 |
| 18 | Stop with `ctrl+c` to your own pane; kill by PID, never `pkill -f` | `SKILL.md:64`, `references/recovery.md:66-75`, `references/failure-modes.md:14` | `pane_send.test_interrupt_is_one_ctrl_c_to_an_own_pane`, `pane_close.test_pane_code_has_no_pattern_kill` | new | 7, 8 |
| 19 | Close only by the gates: done, unchanged 60 minutes, no draft or dialog, unfocused, clean checkout, no outside process; dry first; logged | `SKILL.md:59`, `wsl/reap-done.py:2-11` | `pane_close.test_only_retired_rows_are_candidates`, `pane_close.test_an_hour_unchanged_is_the_candidate`, `pane_close.test_any_change_restarts_the_clock`, `pane_close.test_a_draft_or_an_open_dialog_keeps_the_pane`, `pane_close.test_uncommitted_or_untracked_changes_keep_the_pane`, `pane_close.test_a_live_process_outside_the_pane_keeps_the_checkout`, `pane_close.test_a_change_just_before_close_keeps_the_pane`, `pane_close.test_dry_run_is_the_default`, `pane_close.test_apply_closes_by_pane_id_and_logs_evidence_and_reason` | `test_reap_done.py` | 8 |
| 20 | A live director pane is never closed | `references/dispatch.md:138` | `pane_close.test_live_director_pane_is_never_closed` | `test_focused_control_keep_warden_director_and_lead_panes_are_never_closed` | 8 |
| 21 | One live director; a stale session stands down; a claim grants no pane rights | `SKILL.md:54`, `references/recovery.md:102-109` | `panes.test_second_pane_role_is_held_multi_director_parked`, `panes.test_stale_generation_gets_no_pane_write`, `panes.test_lease_grants_no_pane_write` | `test_coord_claims.py` fencing, already covered by `test_notification_fences` | 10 |
| 22 | A changed occupant stops wakes and leaves the pane | `references/recovery.md:62-64`, `references/supervision.md:130-137` | `panes.test_changed_occupant_stops_wakes_and_keeps_the_pane` | `test_fleet_handoff_and_changed_session_preserve_pane_without_prompt` | 10 |
| 23 | Herdr restart forgets names, pane ids survive; identify by cwd and title | `references/recovery.md:50-64` | `panes.test_herdr_restart_renames_by_pane_and_cwd` | new | 10 |
| 24 | Never `herdr server stop`, never bare `herdr`; tests cannot reach the server | `references/dispatch.md:140`, `SKILL.md:102`, `tests/run.py:19-21` | `herdr_client.test_bare_and_server_commands_are_refused`, `herdr_client.test_tests_cannot_reach_a_real_server` | runner isolation | 5 |
| 25 | Open with `--no-focus`; target explicit ids, never a focused pane | `references/dispatch.md:130-132` | `pane_launch.test_every_pane_opens_without_focus`, `pane_send.test_targets_are_explicit_pane_ids` | new | 7, 9 |
| 26 | Read the pane before the first prompt: self-update or a startup question eats briefs | `references/dispatch.md:133`, `references/failure-modes.md:32` | `pane_launch.test_brief_waits_for_an_idle_composer_and_never_types_into_a_shell`, `pane_launch.test_agent_that_exited_after_self_update_is_refused` | `test_waits_for_idle_and_never_types_into_shell_or_dialog` | 9 |
| 27 | Never prompt with `--wait`; a follow-up to a busy agent may never run | `references/failure-modes.md:26,31` | `pane_send.test_prompts_never_use_wait`, `pane_send.test_busy_agent_gets_no_follow_up` | new | 7 |
| 28 | Verify the footer model and effort after the first turn. The Sol minimum is superseded by "any configured seat" | `SKILL.md:52-53,67-70`, `references/failure-modes.md:23,29` | `pane_launch.test_footer_mismatch_after_first_turn_goes_to_the_inbox`, `config.test_any_configured_seat_needs_no_model_minimum` | `test_orchestrators.py` for the footer read | 1, 9 |
| 29 | `NEEDS RESTART: <path>`. Superseded: Drover has no jail, so a missing root is refused before launch | `references/recovery.md:88-91` | `pane_launch.test_missing_cwd_fails_before_a_pane_opens` | new | 9 |
| 30 | One launch at a time; a stuck launch must not block the rest. Superseded by the single dispatcher thread with a bounded call | `references/recovery.md:25-48` | `panes.test_one_launch_at_a_time_and_a_stuck_launch_leaves_the_tick_running` | new | 10 |
| 31 | Pane scans run on the host, not in a jail. Superseded: only the hub builds a client | `references/supervision.md:44`, `references/failure-modes.md:33` | `panes.test_only_the_hub_constructs_a_herdr_client` | new | 10 |
| 32 | A wake goes only to an unfocused idle pane with an empty composer | `references/supervision.md:130-131`, `wsl/herdr-timers.py:314-319` | `pane_send.test_only_an_unfocused_idle_empty_composer_is_written` | `test_live_bottom_focus_still_blocks_due_wake`, `test_working_focused_and_approval_owner_are_preserved` | 7 |

---

### Task 1: Any configured seat may be Director (Wave 1)

**Files:**
- Modify: `drover/config_validation.py:142-145`, `drover/config.py:82-113,195-200`, `drover/config_defaults.py:74`, `drover/route.py:32-41`, `drover.example.toml:101,129`
- Test: `tests/test_config.py:304,325`, `tests/test_route.py` or the module that covers `default_routes`

**Interfaces:**
- Produces: `config.director_model(cfg=None) -> str`, which is `director.model or models.director`; it must be on the ladder of the director seat's backend.
- Produces config, the only Phase 3 keys anywhere: `[panes] enabled = false`, `herdr_bin = ""`, `socket = ""`, `workspace = ""`, `call_timeout_s = 10.0`, `retry_unavailable_s = 60.0`; `[director] handoff_tokens = 275000`; `[context] file_limit_bytes = 6144`, `total_limit_bytes = 12288`.

- [ ] **Step 1:** Write tests: `test_codex_director_seat_is_accepted`, `test_any_configured_seat_needs_no_model_minimum`, `test_codex_on_windows_is_still_refused`, `test_exactly_one_director_seat_still_required`, `test_default_director_is_opus_on_the_claude_seat`, `test_codex_director_without_director_model_names_the_fix`, `test_director_model_must_be_on_the_seat_ladder`, `test_director_reserve_defaults_to_25_on_any_backend`, `test_default_route_follows_the_director_seat`, `test_director_seats_put_the_director_seat_first`, `test_handoff_tokens_must_be_positive`, `test_panes_default_off`. Change the two old assertions at `tests/test_config.py:304,325` to match.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Remove the check at `config_validation.py:144-145` and keep 146-148. Make the 25% default at `:142-143` apply to any director seat. Replace both "must be on the claude ladder" checks with one on `director_model`. A Codex seat with no `director.model` raises `ConfigError` naming `director.model`; nothing is substituted.
- [ ] **Step 4:** Set `default_routes()["director"]` to `[(seat backend, director_model)]`, and order `director_seats` with the director seat first, then Claude seats, then the rest. Add the new keys to `DEFAULTS` and validate them in `config.py`. Update the comment at `drover.example.toml:129`.
- [ ] **Step 5:** Run `tests.test_config` and the route and dispatcher admission suites; expect OK. Commit.

### Task 2: The screen rules move into Drover (Wave 1)

**Files:**
- Create: `drover/pane_guard.py`, `drover/pane_screen.py`
- Test: `tests/test_pane_guard.py`, `tests/test_pane_screen.py` (new)

**Interfaces:**
- Produces in `pane_guard`: `bottom_region(screen) -> str`, `queued_input_range(screen) -> tuple | None`, `working(screen) -> bool`, `model_prompt(screen) -> bool`, `strong_dialog(screen) -> bool`, `blocking_dialog(screen) -> bool`, `startup_keys(screen, fresh_pane=False) -> tuple | None`, `refuse_dialog(screen, target=None) -> None`, raising `ValueError`.
- Produces in `pane_screen`: `status(screen, kind) -> str | None` (`'idle'`, `'working'`, `'blocked'` or `None`), `composer_text(screen, kind) -> str | None`, `composer_empty(screen, kind) -> bool`, `viewport_held(screen) -> bool`, `viewport_class(pane: dict, screen) -> str | None`, `own_prompt(text, digest, sent_at, now) -> bool`, `prompt_fingerprint(text) -> tuple[str, str]`, `queued_idle(screen) -> bool`, `queued_prompt(screen, prompt) -> bool`, `classify(agent, screen, item, now) -> str | None`, `footer_model(screen) -> tuple | None`, `context_left(screen) -> int | None`. `kind` is `'claude'` or `'codex'`.

- [ ] **Step 1:** Port the tests named in rows 3, 4, 7, 8, 10, 11, 12 and 17, and `test_each_stuck_class`, with their screens copied from `H:\repos\my-skills\herdr\tests`. Drop the launcher cases that belong to Task 9. Add `test_bytes_from_herdr_decode_as_utf8`, feeding `DIALOG` from `test_ui_guard.py:17` as UTF-8 bytes, and `test_unknown_layout_is_none_for_both_kinds`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Port `wsl/ui_guard.py:9-81,102-106` to `pane_guard` as an exact copy of the regexes and no Herdr import. In `startup_keys` keep only the trust branch at `:93-94`: the update branches at `:95-98` stay owner-only. Add `kind` to the signature so a Claude screen returns `None`.
- [ ] **Step 4:** Port `wsl/herdr-timers.py:37-65,72-85,100-137,139-155,159-212,271-280,367-378` to `pane_screen`, without the process-table checks. Claude's idle read is an exact copy of `wsl/send-prompt.py:27-33`. `viewport_class` takes the parsed `pane get` result, not an API. `context_left` reads the footer's `N% context left`.
- [ ] **Step 5:** Run both modules; expect OK. Commit.

### Task 3: A WSL agent reports its own status through herdr.exe (Wave 1)

**Files:**
- Create: `drover/pane_status_hook.py`
- Test: `tests/test_pane_status_hook.py` (new)

**Interfaces:**
- Produces: stdlib-only, runnable by path as `python3 <path> --agent claude|codex`, reading the hook's JSON on stdin. `state_of(event) -> str | None`, `commands(env, agent, event, session, seq) -> list[list[str]]`, `main(argv=None, *, stdin=None, env=None, run=None) -> int`.
- Produces constants: `SOURCE = {'claude': 'herdr:claude-wsl', 'codex': 'herdr:codex-wsl'}`, `WSLENV_NAMES = ('HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH', 'HERDR_BIN_PATH', 'HERDR_WORKSPACE_ID', 'HERDR_TAB_ID')`.

- [ ] **Step 1:** Write tests: `test_outside_herdr_does_nothing`, `test_each_event_maps_to_its_state` (table below), `test_session_start_reports_the_session_then_idle`, `test_session_end_releases_the_agent`, `test_unknown_event_makes_no_call`, `test_bad_json_makes_no_call_and_exits_zero`, `test_windows_binary_path_goes_through_wslpath`, `test_linux_binary_path_is_used_as_is`, `test_claude_and_codex_use_their_own_source`, `test_a_failing_herdr_never_fails_the_hook`, `test_module_imports_only_the_standard_library`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Port `wsl/herdr-wsl-agent-state.sh` as Python: `SessionStart`, `Stop` and `Interrupt` give idle; `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStart`, `SubagentStop`, `PreCompact` and `PostCompact` give working; `PermissionRequest` gives blocked; `SessionEnd` gives release. Issue `pane report-agent-session`, `pane report-agent --state` and `pane release-agent` as in the script's lines 33-42, detached with output discarded, so the agent never waits on Herdr.
- [ ] **Step 4:** Run the module on Windows, then under WSL with `python3`; expect OK. Commit.

### Task 4: Wakes are grouped, and the wake limit is 6 (Wave 1)

**Files:**
- Modify: `drover/notifications/roles_wake.py:68-81,142`, `drover/notifications/roles.py`, `drover/directors.py:356-361`, `hub/notifications.py:79`, `docs/director-notifications.md:37-42,74`
- Test: `tests/test_director_roles.py`, `tests/test_directors.py`, `tests/test_notification_api.py`

**Interfaces:**
- Produces: `DEFAULT_WAKE_LIMIT = 6`, `DEFAULT_WAKE_WINDOW_S = 600`, `DEFAULT_URGENT_WINDOW_S = 60`; policy fields `wake_window_s`, `urgent_window_s`, whole numbers 1 to 86400; `roles.wake_due(service, role_id) -> bool`.

- [ ] **Step 1:** Write tests: `test_default_wake_limit_is_6`, `test_hub_policy_default_is_6`, `test_burst_of_events_gives_one_wake_per_window`, `test_empty_history_wakes_at_once`, `test_critical_delivery_uses_the_urgent_window`, `test_policy_windows_override_the_defaults`, `test_window_fields_are_validated`, `test_burst_urgent_and_cap_keep_the_wake_pending_with_one_question`. The last sends 20 events and one critical issue over an hour on a fake clock and asserts wakes `<= 6`, one open question, and `pending_wake == 1` afterward.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `wake_due`: the time since the latest `director_budget_entries` row with `wakes > 0` is at least the window. The window is `urgent_window_s` when an unacknowledged `notification_inbox` envelope has `severity == 'critical'`, else `wake_window_s`. It needs no schema change. Skip the role in `tick` when not due, before budget is reserved.
- [ ] **Step 4:** Replace the literal 4 at `roles_wake.py:142` and `hub/notifications.py:79`, and update the defaults table. Tests that pass `wake_limit_per_hour=4` explicitly stay.
- [ ] **Step 5:** Run `tests.test_director_roles tests.test_directors` and the notification suites under the hub venv; expect OK. Commit.

### Task 5: One client reaches Herdr, and Drover records what it started (Wave 2)

**Files:**
- Create: `drover/herdr_client.py`, `drover/ledger/ops_panes.py`, `tests/fake_herdr.py`
- Modify: `drover/ledger/schema.py` (new `V19`, appended to `MIGRATIONS`), `drover/ledger/ops.py`, `tests/__init__.py`
- Test: `tests/test_herdr_client.py`, `tests/test_ops_panes.py` (new)

**Interfaces:**
- Consumes: `[panes]` from Task 1.
- Produces in `herdr_client`: `HerdrUnavailable(RuntimeError)`, `HerdrRefused(RuntimeError)` with `.code`; `Herdr(binary, socket='', *, enabled=True, timeout_s=10.0, retry_s=60.0, run=subprocess.run, clock=time.monotonic)` with `call(*argv) -> dict` (the JSON `result`), `text(*argv) -> str`, `available() -> bool`; `from_config(cfg=None) -> Herdr`.
- Produces `V19`: `pane_agents(id INTEGER PRIMARY KEY, role_id TEXT, run_id INTEGER, pane_id TEXT NOT NULL, terminal_id TEXT, name TEXT, kind TEXT NOT NULL CHECK(kind IN ('claude','codex')), seat TEXT NOT NULL, placement TEXT NOT NULL CHECK(placement IN ('windows','wsl')), session_id TEXT, cwd TEXT, started_by TEXT NOT NULL CHECK(started_by IN ('drover','owner')), state TEXT NOT NULL, handoff_state TEXT, context_tokens INTEGER, created TEXT NOT NULL, updated TEXT NOT NULL)` and `pane_writes(id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL, head TEXT, state TEXT NOT NULL, reason TEXT, at TEXT NOT NULL)`. `pane_agents.state` is one of `starting`, `live`, `rotating`, `compaction`, `retired`, `gone`.
- Produces in `ops_panes`: `record_agent(conn, **fields) -> dict`, `update_agent(conn, agent_id, **fields) -> dict`, `open_agents(conn, *, placement=None) -> list[dict]`, `by_pane(conn, pane_id) -> dict | None`, `note_write(conn, agent_id, kind, text) -> dict`, `finish_write(conn, write_id, state, reason=None) -> dict`, `last_write(conn, agent_id, kind=None) -> dict | None`.
- Produces in `tests/fake_herdr.py`: `FakeHerdr(agents, screens, panes)`, used as the `run` argument, recording every argv.

- [ ] **Step 1:** Write tests: rows 1 and 24, plus `test_only_allowlisted_verbs_reach_the_binary`, `test_output_decodes_as_utf8`, `test_unavailable_is_remembered_for_the_retry_window`, `test_timeout_raises_unavailable`, `test_refusal_keeps_its_code`, `test_no_window_on_windows`, `test_write_stores_a_digest_and_a_short_head_not_the_text`, `test_seat_and_placement_are_required`, `test_v19_applies_to_a_copy_of_a_v18_ledger`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `Herdr`. The allowlist is `agent` (`get`, `list`, `read`, `prompt`, `send-keys`, `start`, `rename`), `pane` (`get`, `list`, `read`, `split`, `run`, `send-text`, `send-keys`, `close`, `process-info`, `report-agent`, `report-agent-session`, `release-agent`), `tab` (`create`, `list`) and `workspace` (`list`). Anything else raises `ValueError` before a process starts. Pass `encoding='utf-8', errors='replace'`, the configured socket as `HERDR_SOCKET_PATH`, and `placement_exec.NO_WINDOW`.
- [ ] **Step 4:** Add `V19` and `ops_panes`. In `tests/__init__.py`, point `HERDR_SOCKET_PATH` at a path that cannot exist, as the skill's `tests/run.py:19-21` does, and clear `HERDR_ENV`.
- [ ] **Step 5:** Run `tests.test_herdr_client tests.test_ops_panes` and the ledger suite; expect OK. Commit.

### Task 6: Fresh agents load only short matching files (Wave 2)

**Files:**
- Create: `drover/context_files.py`
- Modify: `drover/brief.py:114-147`
- Test: `tests/test_context_files.py` (new), `tests/test_brief.py`

**Interfaces:**
- Consumes: `[context]` from Task 1.
- Produces: `applies_to(text) -> list[str]`; `select(root, scope, *, file_limit, total_limit) -> dict`, keys `files` (`[{'path', 'text'}]`) and `skipped` (`[{'path', 'reason'}]`).

- [ ] **Step 1:** Write tests on a temporary repo: `test_empty_scope_loads_nothing`, `test_only_accepted_adrs_with_a_matching_glob_load`, `test_malformed_front_matter_is_skipped_not_fatal`, `test_a_file_over_the_limit_is_skipped_whole`, `test_total_limit_drops_whole_files_in_a_stable_order`, `test_narrower_glob_wins_over_a_broad_one`, `test_newest_adr_wins_a_tie`, `test_no_glossary_readme_or_design_doc_is_ever_loaded`, `test_a_missing_adr_folder_loads_nothing`, `test_a_read_failure_never_stops_the_brief`, `test_this_repo_stays_under_the_limit_for_a_drover_scope`; in `tests.test_brief`, `test_matching_files_appear_as_their_own_section` and `test_context_files_drop_before_the_brief_is_refused`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `applies_to` by `json.loads` on the text after `applies_to:`, since it is a flow list and no YAML dependency is allowed. Match with `PurePosixPath.full_match`. Read only `docs/adr/*.md` with `status: accepted`.
- [ ] **Step 4:** Order by longest literal prefix of the matching glob, then ADR id descending. Stop at `total_limit`; every file is whole or absent. Go through `placement.git(root, 'show', 'HEAD:<path>')` for a WSL worktree, and read nothing while `compaction_hold.active()`.
- [ ] **Step 5:** In `assemble`, add a "Decisions that apply" section after "Repo instructions". Trim it, whole files last to first, before notes are trimmed and before `BriefTooLarge`.
- [ ] **Step 6:** Run `tests.test_context_files tests.test_brief`; expect OK. Commit.

### Task 7: One guarded path writes to a pane (Wave 3)

**Files:**
- Create: `drover/pane_send.py`
- Test: `tests/test_pane_send.py` (new)

**Interfaces:**
- Consumes: `pane_guard`, `pane_screen`, `Herdr`, `ops_panes`.
- Produces: `PaneRefused(Exception)` with `.reason`; `check(herdr, row) -> dict`, keys `agent`, `pane`, `screen`; `send(port, herdr, row, text, kind) -> dict`; `submit_own(port, herdr, row) -> dict`; `interrupt(port, herdr, row, kind='interrupt') -> dict`; `send_stop(port, herdr, row, text, hold_id) -> dict`. `kind` is `brief`, `wake`, `handoff`, `resume`, `stop` or `interrupt`.

- [ ] **Step 1:** Write the tests in rows 2, 3, 4, 6, 11, 13, 14, 17, 18, 25, 27 and 32 for `pane_send`, plus: `test_focus_between_final_read_and_send_sends_nothing`, `test_typing_between_paste_and_enter_withholds_the_enter_and_never_resends` (Review Focus), `test_intent_is_recorded_before_the_write`, `test_herdr_unavailable_is_a_refusal_not_an_exception`, `test_stop_needs_a_drover_row_in_wsl_and_a_live_hold`, `test_stop_skips_a_dialog_or_a_draft`, `test_stop_reaches_a_working_or_focused_pane`, `test_only_compaction_calls_send_stop`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `check`: refuse without a row where `started_by == 'drover'`; then an unchanged `pane_id`, `terminal_id` and session; `pane_guard.refuse_dialog`; `viewport_class`; unfocused; status idle on both Herdr and screen; empty composer. Unknown refuses. Every refusal is a `PaneRefused`, never a keystroke.
- [ ] **Step 4:** Implement `send`: run `check` twice with the screens compared, write `ops_panes.note_write`, then a Windows agent gets `agent prompt <pane> <text>` without `--wait`, and a WSL agent gets `pane send-text` with one bracketed paste. After the paste, wait 0.8 s, run `check`-style reads again, and confirm the composer holds this text by `own_prompt`; only then `pane send-keys <pane> enter`. Any failure ends the write as `unverified` or `unsent`; nothing is resent.
- [ ] **Step 5:** Implement `submit_own`, one Enter only when the composer matches the digest of the row's last write. Implement `interrupt`, one `ctrl+c` by `pane send-keys`, after refusing any dialog.
- [ ] **Step 6:** Implement `send_stop`, the single exception: it drops only the idle and unfocused tests. It still requires a Drover row with `placement == 'wsl'` and `hold_id` equal to the live hold, and refuses a dialog or draft, because Enter could confirm a selected option or submit the owner's text.
- [ ] **Step 7:** Run `tests.test_pane_send`; expect OK. Commit.

### Task 8: Drover closes only its own finished panes (Wave 3)

**Files:**
- Create: `drover/pane_close.py`
- Test: `tests/test_pane_close.py` (new)

**Interfaces:**
- Consumes: `pane_screen`, `Herdr`, `ops_panes`; `placement_proof` for process visibility.
- Produces: `judge(row, agent, screen, facts, *, now, observed) -> dict`, keys `verdict` (`'close'`, `'wait'`, `'keep'`) and `reason`; `tick(port, herdr, now, *, apply=True) -> list[dict]`. `MIN_IDLE_S = 3600`.

- [ ] **Step 1:** Port the 17 cases of `tests/test_reap_done.py`, plus the tests in rows 2, 18 and 20. Add `test_only_retired_rows_are_candidates` and `test_unknown_process_visibility_keeps_the_pane`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Port `wsl/reap-done.py:63-74,98-151,184-204`, with these changes: a pane is a candidate only when its `pane_agents.state` is `retired`; "owner's other panes" have no path at all; the process check goes through `placement_proof`; the log is a `pane-closed` ledger event naming evidence and reason, plus the row moving to `gone`. The re-read before `pane close` stays.
- [ ] **Step 4:** Run `tests.test_pane_close`; expect OK. Commit.

### Task 9: Drover starts an agent in a pane (Wave 4)

**Files:**
- Create: `drover/pane_launch.py`
- Modify: `drover/launch.py:496-500` (remove the `PaneLauncher` stub and its assertion at `tests/test_dispatcher_admission.py:167`)
- Test: `tests/test_pane_launch.py` (new)

**Interfaces:**
- Consumes: `pane_send`, `pane_guard`, `pane_screen`, `Herdr`, `ops_panes`, `pane_status_hook.WSLENV_NAMES`, `launch._reject_permission_bypass`, `config.seat_env`, `placement.to_wsl_path`.
- Produces: `write_private(path, text, *, placement, run=subprocess.run) -> str`; `repo_trusted(cwd, seat, *, read=None) -> bool`; `open_pane(herdr, cfg, cwd) -> str`; `start(port, herdr, spec, credential, guard, *, cfg) -> dict`, the `pane_agents` row; `resume(port, herdr, row, spec, *, cfg) -> dict`; `brief(spec, handoff=None) -> str`.
- Produces constants: `NATIVE_COMPACT = 350_000`, `FIRST_TURN_S = 90`.

- [ ] **Step 1:** Write the tests in rows 6, 9, 10, 25, 26, 28 and 29, plus: `test_credential_never_reaches_argv_scrollback_or_a_write`, which scans every recorded argv and sent text for the credential; `test_wsl_credential_file_is_private_on_ext4`; `test_windows_credential_file_has_a_private_acl`; `test_wsl_launch_sets_wslenv_and_runs_through_wsl_exe`; `test_windows_launch_uses_agent_start_after_env`; `test_agent_is_renamed_once_it_reports`; `test_seat_and_placement_are_written_before_any_input`; `test_session_id_is_recorded_at_session_start`; `test_permission_bypass_is_refused`; `test_native_compaction_sits_above_the_forced_handoff`; `test_codex_update_check_is_off`; `test_hub_unreachable_from_wsl_holds_before_any_effect`; `test_herdr_closed_raises_admission_held`; `test_claude_folder_trust_reports_to_the_owner_and_starts_nothing_else`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `write_private`. On Windows, create with `O_EXCL` and restrict by `icacls <path> /inheritance:r /grant:r <user>:F`. For WSL, pipe the text on stdin into a `0600` file under `~/.drover/panes/` through `placement.wsl_control`: a drvfs path cannot pass the CLI's owner check. Only the path crosses the pane.
- [ ] **Step 4:** Implement `repo_trusted`: read the seat's `CODEX_HOME/config.toml` with `tomllib`, map the worktree to its main checkout with `git rev-parse --git-common-dir`, compare in WSL path form, and require `trust_level = "trusted"`. Any error is `False`.
- [ ] **Step 5:** Implement `open_pane`: `tab create --workspace <id> --cwd <cwd> --no-focus`, with `workspace` from `[panes]` or the first of `workspace list`. No workspace raises `AdmissionHeld('herdr-no-workspace')`.
- [ ] **Step 6:** Implement `start`. Fail before any effect on a missing cwd, on `placement == 'wsl'` with `api_listen_wsl` off, or on `HerdrUnavailable`, each as `AdmissionHeld`. Then record the row and write a launcher file under `<state>/panes/<run_id>/` holding the seat env, `DROVER_HUB_URL`, `DROVER_CREDENTIAL_FILE` and, for WSL, `WSLENV`. `pane run` carries only the launcher's path. Windows uses `agent start <name> --kind <kind> --pane <pane> -- <args>`. WSL runs `wsl.exe -d <distro> -e bash -li <script>` from the launcher, and renames the agent once the hook reports it.
- [ ] **Step 7:** Args: model and effort; never a permission flag beyond the seat's own settings, passed through `_reject_permission_bypass`. Claude gets `--autocompact 350k` and a `--settings` file with the Task 3 hook for a WSL placement. Codex gets `-c model_context_window=350000 -c model_auto_compact_token_limit=350000 -c check_for_update_on_startup=false`.
- [ ] **Step 8:** Startup: read the pane; a shell prompt with no agent fails; a fresh-pane Codex trust dialog with `repo_trusted` true gets the single `enter` from `pane_guard.startup_keys`, recorded as a `pane-trust-answered` event; any other dialog publishes one `pane.notice` and leaves the pane. Send `brief()` once through `pane_send.send`, then require working or blocked activity within `FIRST_TURN_S`. Verify `footer_model` after the first turn; a mismatch is a notice, never a restart.
- [ ] **Step 9:** `resume` relaunches the recorded session in the same pane: `claude --resume <id>` or `codex resume <id>`, same seat and placement, with no brief.
- [ ] **Step 10:** Run `tests.test_pane_launch tests.test_dispatcher_admission`; expect OK. Commit.

### Task 10: A director runs as a resident pane run (Wave 5)

**Files:**
- Create: `drover/panes.py`, `drover/pane_wake.py` and `drover/pane_handoff.py` (each with one empty function for Tasks 11 and 12)
- Modify: `drover/directors.py:82-113,163-200,296-326,344-412`, `drover/notifications/roles_auth.py`, `drover/notifications/roles.py`, `hub/dispatch.py:195-202`, `drover/dispatcher.py:76-104`
- Test: `tests/test_panes.py` (new), `tests/test_directors.py`

**Interfaces:**
- Consumes: Tasks 5, 7, 8 and 9.
- Produces in `panes`: `PaneService(port, herdr, *, cfg=None, now=time.time)`, with `maintain(run) -> None` and `compaction_tick(hold) -> dict`; `PaneDirectorAdapter(service)`, with `launch(spec, credential, guard)`, `verify_resume(spec)`, `probe(spec)`, `adopt(spec)`, `stop(spec)`, `maintain(run)` and `capabilities`; `PaneHandle.poll() -> dict | None`, which is `None` while the agent lives.
- Produces: `capabilities = dict(resident=True, live_offer=True, verified_resume=True, fresh_start=True, enforced_token_cap=False, authenticated_processing=True, secure_handoff=True)`; `session_ttl = 86400`.
- Produces in `directors`: `CLEAN_OUTCOMES = ('done', 'stopped', 'admission-held', 'rotated', 'pane-gone', 'compaction-fresh')`, replacing the tuple at `:392`; `DispatchRuntime.panes`.
- Produces: `roles.renew_session(service, role_id, session_id, ttl=86400) -> dict`, extending `director_sessions.expires` with no generation change.
- Produces hooks: `pane_wake.deliver(service, run) -> None`, `pane_handoff.maintain(service, run) -> None`, both empty here; `pane_close.tick` is called from `PaneService.maintain`.

- [ ] **Step 1:** Write the tests in rows 21, 22, 23, 30 and 31, plus: `test_resident_run_needs_no_token_cap_capability`, `test_runner_adapter_still_needs_all_three`, `test_resident_launch_reserves_no_tokens`, `test_execution_stays_off_by_default`, `test_session_renews_before_expiry_without_a_new_generation`, `test_owner_started_pane_has_no_row_and_gets_no_input`, `test_probe_is_unknown_when_herdr_is_closed`, `test_herdr_closed_does_not_spend_the_replacement`, `test_pane_gone_with_herdr_up_finishes_as_pane_gone`, `test_a_restart_of_herdr_keeps_the_generation`, `test_hub_tick_with_herdr_closed_dispatches_workers_and_publishes_one_notice` (Review Focus), `test_no_exception_leaves_maintain`, `test_panes_disabled_keeps_the_runner_adapter`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `probe`: `HerdrUnavailable` is `unknown`. With Herdr up, a missing pane or a changed `terminal_id` is `gone`, and the row moves to `gone`. A row in `compaction` is `alive`. A live occupant whose session differs stops wakes and publishes one notice; the pane stays. Names lost to a restart are restored with `agent rename` when pane id and cwd match.
- [ ] **Step 4:** In `DirectorRuntime`: for a `resident` adapter, `required` drops `enforced_token_cap` and `_start` reserves `tokens=0`, because an interactive CLI cannot enforce it and the context ceiling is the real limit. Pass `ttl=getattr(adapter, 'session_ttl', 3600)` to `issue_session`. Use `spec['sandbox']` from the seat for a resident run, keeping `_reject_permission_bypass`. Call `adapter.maintain(run)` for each active run inside `try/except Exception` that logs, and hold a second pane role with `multi-director-parked`.
- [ ] **Step 5:** Wire it in `hub/dispatch.py`: `[panes] enabled` true builds `PaneService` and `PaneDirectorAdapter`, else `RunnerDirectorAdapter` as now. Only that function constructs a `Herdr`. `director_execution_enabled` keeps its `'false'` default.
- [ ] **Step 6:** Run `tests.test_panes tests.test_directors tests.test_director_roles`, and the hub dispatch suites under the hub venv; expect OK. Commit.

### Task 11: A due wake reaches a safe pane, or stays in the mailbox (Wave 6)

**Files:**
- Modify: `drover/pane_wake.py`
- Test: `tests/test_pane_wake.py` (new)

**Interfaces:**
- Consumes: `roles.wake_due`, `service.reserve_wake`, `pane_send.send`, `pane_screen.queued_prompt`, `pane_screen.queued_idle`, `disk_notices.publish(..., kind='pane.notice', epic_title='Directors', actor='panes')`.
- Produces: `deliver(service, run) -> None`; `TEXT = 'Drover: new notices. Read your inbox and commit a checkpoint.'`; `UNVERIFIED_S = 900`, `WEDGED_S = 600`.

- [ ] **Step 1:** Write the tests in rows 5, 8, 15 and 16, plus: `test_no_wake_during_a_compaction_hold`, `test_focused_pane_keeps_the_wake_pending`, `test_one_wake_outstanding_at_a_time`, `test_a_refused_send_spends_no_budget`, `test_wake_not_acknowledged_in_15_minutes_is_one_notice_and_no_resend`, `test_owner_draft_is_never_noticed`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `deliver`. Do nothing unless `pending_wake`, `wake_due`, no hold, and no outstanding wake write. Run `pane_send.check` before reserving budget; then `reserve_wake('wake:<wake_position>')`, then `send(..., kind='wake')`. Mail stays in the inbox throughout.
- [ ] **Step 4:** Verify on later ticks. Observed working or blocked activity moves the write to `observed`; a higher `checkpoint_revision` moves it to `acknowledged`, the only state counted as a delivered wake. `queued_prompt` holds it as queued. Past `UNVERIFIED_S` it becomes `wake_unverified`, with one notice. An idle queue past `WEDGED_S` gets one notice and no key.
- [ ] **Step 5:** Run `tests.test_pane_wake`; expect OK. Commit.

### Task 12: A director hands off its session before it fills (Wave 6)

**Files:**
- Create: `drover/handoff.py`, `drover/pane_context.py`
- Modify: `drover/pane_handoff.py`, `drover/watch.py`, `drover/notifications/roles.py` (`wake_due` after `rotated`), `drover/pane_launch.py` (`brief()` only)
- Test: `tests/test_handoff.py`, `tests/test_pane_handoff.py` (new), `tests/test_watch.py`

**Interfaces:**
- Consumes: `[director] handoff_tokens`, `[watch] context_tokens` as the forced level, `pane_send.send`, `roles.signal`, `DirectorRuntime.finish`.
- Produces in `handoff`: `SECTIONS = ('Goal', 'Done', 'Decisions and rejected options', 'Standing rules', 'Files touched', 'Unverified work', 'Next step', 'What not to redo')`; `request_text(path) -> str`; `missing(text) -> list[str]`; `fallback(role_id, state, ledger_facts) -> str`.
- Produces in `pane_context`: `read(row, herdr, cfg) -> int | None`.
- Produces: `pane_handoff.maintain(service, run) -> None`. `handoff_state` moves through `requested`, `written`, `rotated`.

- [ ] **Step 1:** Write tests: `test_context_is_the_last_real_usage_of_the_transcript`, `test_sidechain_and_synthetic_messages_are_ignored`, `test_boundary_after_the_last_message_reports_post_compaction_size`, `test_codex_context_comes_from_the_footer_percent`, `test_no_transcript_or_footer_is_unknown`, `test_unknown_context_requests_nothing_and_rotates_nothing` (Review Focus), `test_wsl_transcript_is_not_read_during_a_hold`, `test_handoff_is_requested_once_at_275k`, `test_handoff_tokens_is_configurable`, `test_unsafe_pane_retries_on_a_later_tick_without_a_second_write`, `test_valid_file_rotates_at_the_next_idle_moment`, `test_missing_section_is_not_valid`, `test_at_300k_drover_writes_its_own_handoff_and_rotates_when_safe`, `test_at_300k_a_working_or_focused_pane_gets_no_input_and_one_notice`, `test_no_compact_is_ever_sent`, `test_owner_compact_cancels_the_request`, `test_rotation_needs_no_replacement_budget`, `test_old_session_is_fenced_and_its_pane_retired`, `test_new_session_gets_the_file_and_restates_constraints_first`, `test_missing_restatement_is_one_notice`; in `tests.test_watch`, `test_a_director_over_300k_opens_the_context_notice`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `pane_context.read`. Claude: port `wsl/context-watch.py:44-56,85-125` over `<config_dir>/projects/*/<session>.jsonl`. Codex: `int(350000 * (100 - pane_screen.context_left(screen)) / 100)`. Anything unreadable is `None`. Skip WSL paths while `compaction_hold.active()`.
- [ ] **Step 4:** Implement `handoff.py`. The file is `<state>/directors/<role_id>/handoff-<generation>.md`, passed in WSL form for a WSL agent. `fallback` is built from the checkpoint and open tasks, and marks every section it cannot fill as `unknown`. `launch_usage.rotation_handoff` stays for headless workers.
- [ ] **Step 5:** Implement `maintain`. Under `handoff_tokens`, clear any `requested` state and return. At or above it, once, send `request_text` through `pane_send.send`. When the file passes `missing` and `pane_send.check` passes, rotate. At or above the forced level with no valid file, write `fallback` and rotate on the first passing `check`. Until then `PaneRefused` means no input, and the Phase 2 context notice carries the state: feed `pane_agents.context_tokens` to `watch.context`.
- [ ] **Step 6:** Rotate by `DirectorRuntime.finish(run_id, status='rotated')`, moving the row to `retired`, then `roles.signal` so the next tick starts a fresh run with `brief(spec, handoff=path)`. `wake_due` is true when the role's last outcome is `rotated`. The new credential fences the old session, nothing is typed into the old pane, and Task 8 closes it. The brief requires `restated.md` beside the handoff before other work.
- [ ] **Step 7:** Run `tests.test_handoff tests.test_pane_handoff tests.test_watch`; expect OK. Commit.

### Task 13: The compaction stop reaches panes, and agents resume (Wave 6)

**Files:**
- Create: `drover/pane_compaction.py`
- Modify: `drover/compaction.py:29-30,171-210,213-245,143-152`, `drover/compaction_hold.py`, `drover/dispatcher.py:181-185`, `drover/panes.py` (`compaction_tick` only), `docs/operations/disk-budget-rollout.md:32`
- Test: `tests/test_pane_compaction.py` (new), `tests/test_compaction.py`

**Interfaces:**
- Consumes: `pane_send.send_stop`, `pane_send.interrupt`, `pane_send.send`, `pane_launch.resume`, `DirectorRuntime.finish`, `roles.signal`.
- Produces in `compaction_hold`: `PANE_FILE = 'compaction-panes.json'`, `ASK_S = 300`, `INTERRUPT_S = 60`, `save_panes(state_dir, answer)`, `read_panes(state_dir=None) -> dict | None`, removed with the hold.
- Produces in `compaction`: `pane_stop(runtime) -> dict`, `pane_resume(runtime) -> dict`, `Runtime.wait_for_panes(phase, deadline_s) -> dict`; `PANE_ANSWER_S = 120`, `PANE_STOP_WAIT_S = 420`, `PANE_RESUME_WAIT_S = 240`. Phase `resuming` between `offline` and release.
- Produces: `pane_compaction.tick(service, hold) -> dict`, `id`, `phase`, `settled`, `agents` (`[{'pane_id', 'seat', 'placement', 'session_id', 'outcome', 'reason'}]`). `outcome` is one of `stopped-asked`, `stopped-interrupted`, `left-for-kill`, `resumed`, `resume-failed`.

- [ ] **Step 1:** Write tests: `test_only_drover_wsl_rows_are_touched`, `test_windows_claude_director_is_left_alone`, `test_codex_director_is_stopped_as_a_wsl_pane_agent`, `test_stop_now_goes_out_once_and_the_interrupt_after_300s`, `test_kill_is_reached_at_360s`, `test_dialog_or_draft_skips_text_and_interrupt_and_waits_for_the_kill`, `test_timings_survive_a_hub_restart`, `test_job_goes_on_when_the_hub_never_answers`, `test_stop_and_drain_fit_the_deadlines`, `test_session_id_is_in_the_answer_before_the_kill`, `test_compaction_kill_does_not_spend_the_replacement`, `test_resume_sends_restart_complete_keep_going`, `test_resume_uses_the_recorded_seat_and_placement`, `test_codex_resume_failure_flags_seat_and_placement_and_starts_fresh` (Review Focus), `test_fresh_agent_starts_only_after_the_hold_is_released`, `test_herdr_closed_is_one_flag_and_the_job_finishes`, `test_retired_panes_are_not_resumed`, `test_report_names_stopped_killed_resumed_and_reclaimed`, `test_a_clean_night_with_a_failed_resume_still_raises_a_notice`, `test_wsl_shutdown_step_is_unaffected`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3:** Implement `pane_compaction.tick` for the stop phase. Timings count from `hold['since']`, so a hub restart does not start them over. On the first tick for a row in `live`, move it to `compaction` and `send_stop(..., 'stop now', hold['id'])`. At `ASK_S`, if the row is not idle, `interrupt` once. A `PaneRefused` records `left-for-kill`. Save the answer each tick. `settled` is true when every row is idle or `left-for-kill`, or at `ASK_S + INTERRUPT_S`.
- [ ] **Step 4:** Fill `pane_stop(runtime)`: read the hold, `wait_for_panes('stop', PANE_STOP_WAIT_S)`, and return early with an error when no answer exists after `PANE_ANSWER_S`. `_compact` copies the result to `result['panes']`, tolerating `None` for the mock at `tests/test_compaction.py:153`. `runtime.kill` stays the kill step. The drain wait after it sees runs the hub stopped at 300 s.
- [ ] **Step 5:** In `_held`, after a successful `restart`, record step `resume` with phase `resuming`, then `pane_resume(runtime)` before release, inside a `try` so a failure never stops the release. After a failed restart, skip it and flag every row.
- [ ] **Step 6:** Implement the resume phase in `tick`: `pane_launch.resume`, then, once idle, `pane_send.send(..., 'restart complete, keep going', 'resume')`. On any failure the row is `resume-failed` with its reason, `finish(status='compaction-fresh')` runs, and `roles.signal` is sent so the role's next tick, after the release, starts a fresh agent from the repo, ledger and brief.
- [ ] **Step 7:** Extend `report()`: the file carries `panes`, and a `resume-failed` row raises the notice even on an otherwise clean night, naming seat and placement. Add a dispatcher call to `self.panes.compaction_tick(hold)` in the `if hold:` branch, in a `try`. Replace the note at `disk-budget-rollout.md:32`.
- [ ] **Step 8:** Run `tests.test_pane_compaction tests.test_compaction`; expect OK. Commit.

### Task 14: The glossary retires the skill's words (Wave 7)

**Files:**
- Modify: `GLOSSARY.md:90,313-335`, `docs/configuration.md`, `README.md`, `docs/design.md` if it names a stale director rule, `docs/director-notifications.md:4-6,212-236`, `drover/directors.py:1-8`
- Create: `docs/operations/directors-rollout.md`
- Test: `tests/test_vocabulary.py` (new)

**Interfaces:**
- Produces: `test_vocabulary` scans `drover/pane*.py`, `drover/handoff.py`, `drover/panes.py` and `hub/templates` for visible strings.

- [ ] **Step 1:** Write `test_vocabulary`: `test_retired_words_are_absent_from_pane_copy`, matching `\blane\b`, `\breceipt\b`, `\bgarden\b`, and uppercase `READY`, `LAND`, `HOLD` as whole words. `gate` is checked only in visible strings. The lowercase task state `ready` and the internal `gate` run role are real terms and are not matched.
- [ ] **Step 2:** Run it and confirm what it finds; fix copy in the pane modules.
- [ ] **Step 3:** Fix `GLOSSARY.md:90` ("a lane or a session"). Add retired-term rows for "lane -> worker or pane", "READY -> checked" where the table lacks them. Add entries for Pane, Director pane and Handoff file. Update the Director entry for one interactive director and the Compaction stop entry for resume. Keep implementation out.
- [ ] **Step 4:** Document `[panes]`, `[director] handoff_tokens`, `[context]` and the policy windows in `docs/configuration.md`. Update the "Live offers" and default-adapter paragraphs in `director-notifications.md`, and the module docstring in `directors.py`.
- [ ] **Step 5:** Write `directors-rollout.md`, the owner steps in order: install the Codex hook into the seat's `CODEX_HOME`; enable `api_listen_wsl` for a WSL Director; set `[panes] enabled`; register one role and `director_execution`; set `director_execution_enabled`; run the verification list in Task 15 Step 5.
- [ ] **Step 6:** Run `tests.test_vocabulary`; expect OK. Commit.

### Task 15: Rule coverage and whole-branch verification (Wave 8)

**Files:**
- Create: `tests/test_pane_rule_coverage.py`
- Test: existing suites

- [ ] **Step 1:** Write `test_every_rule_names_a_test_that_exists`: a dict of the 32 rows to dotted test names, resolved with `unittest.TestLoader().loadTestsFromName`.
- [ ] **Step 2:** Run every `tests.test_*` module on Windows with `DROVER_TESTS=integration`, hub suites under the hub venv. Expect OK; report any existing failure by name with its base-commit result.
- [ ] **Step 3:** Run `tests.test_pane_status_hook tests.test_pane_guard tests.test_pane_screen tests.test_pane_send tests.test_directors` under WSL on ext4 in a temporary clone under `~/work`.
- [ ] **Step 4:** Migrate a copy of the live ledger (`C:\Users\johnm\.drover`) in the scratchpad, never the original, and confirm V19 applies and tasks still list.
- [ ] **Step 5:** Commit. Report the owner's first real run, which no test can stand in for: the hub reaches the owner's Herdr server from its scheduled task; a Claude Director starts, wakes and hands off in a Windows pane; a Codex Director reports status through `herdr.exe` with `WSLENV`; one compaction stops and resumes it; and with Herdr closed, dispatch carries on.

### Task 16: Retire the skill (Wave 9)

**Needs owner confirmation before any step runs.** `H:\repos\my-skills` is edited by no other task. Each group has its own confirmation.

**Files:** `H:\repos\my-skills\herdr` only. Delete by path, under each group's condition.

**Group A, delete when Phase 3 is deployed and its first real run passes:**
- `PromptSafety.ps1`, `Send-AgentPrompt.ps1`, `Start-CodexAgent.ps1`
- `wsl/ui_guard.py`, `wsl/send-prompt.py`, `wsl/herdr-timers.py`, `wsl/herdr-progress.service`, `wsl/herdr-supervise.py`, `wsl/herdr-wsl-agent-state.sh`, `wsl/install-wsl-codex.sh`, `wsl/context-watch.py`, `wsl/reap-done.py`, `wsl/launch-lane.py`, `wsl/make-lane.sh`, `wsl/lane.py`, `wsl/launch-lead.sh`, `wsl/lead.py`, `wsl/private_codex_home.py`, `wsl/coord.py`, `wsl/herdr-coord`, `wsl/coord_claims.py`
- `tests/test_ui_guard.py`, `tests/test_timers.py`, `tests/test_timer_live.py`, `tests/test_completion_notifications.py`, `tests/test_launch_regressions.py`, `tests/test_launchers.py`, `tests/test_compaction_policy.py`, `tests/test_private_codex_home.py`, `tests/test_reap_done.py`, `tests/test_context_watch.py`, `tests/test_coord.py`, `tests/test_coord_cli.py`, `tests/test_coord_claims.py`
- `references/supervision.md`, `references/dispatch.md`, `references/context.md`, `references/recovery.md`, `references/lane.md`, `references/lead.md`, `references/director.md`, `references/orchestrator.md`, `references/fleet.md`, `references/coordination.md`, `references/migration.md`, `references/two-tier.md`, `references/two-tier-templates.md`, `references/three-tier.md`, `references/three-tier-templates.md`, `references/failure-modes.md`
- The matching rows of `SKILL.md`'s routing table.

**Group B, Phase 1 and 2 capabilities, due at their own cutover if still present:**
- `wsl/garden.py`, `references/garden.md`, `tests/test_garden.py`
- `wsl/admit-slice.py`, `wsl/admit_checks.py`, `tests/test_admit_slice.py`
- `wsl/warden-check.py`, `wsl/launch-warden.sh`, `wsl/warden/`, `wsl/warden-config.json`, `references/warden.md`, `tests/test_warden.py`
- `wsl/velocity.py`, `references/velocity-and-churn.md`, `tests/test_velocity.py`

**Group C, later than cutover:**
- After 3 clean applied days of Drover cleanup: `wsl/disk_guard.py`, `wsl/disk-guard.sh`, `Install-DiskGuardTask.ps1`, `tests/test_disk_guard.py`. The owner unregisters the `HerdrDiskGuard` task first.
- After a verified real cross-side run: `wsl/lock-shared-worktrees.sh`, `wsl/repair-orphan-worktrees.sh`, `wsl/herdr-hygiene.sh`, `tests/test_hygiene_lock.py`, `tests/test_worktree_lock.py`, `tests/test_repair_orphan_worktrees.py`.

**Group D, the skill itself, last.** These have no cutover in the spec or are imported by surviving scripts:
- `Test-CodexSandbox.ps1`, `wsl/Get-WslBranch.ps1`, `wsl/admission.sh`, `wsl/audit-deployment.sh`, `wsl/channel_cli.py`, `wsl/channels.py`, `wsl/codex-run.py`, `wsl/context_policy.py`, `wsl/coord_slots.py`, `wsl/coord_store.py`, `wsl/dotnet-one-node.sh`, `wsl/dotnet_lane.py`, `wsl/fleet.py`, `wsl/gate-lease.py`, `wsl/gate-receipt.py`, `wsl/gate-scope.py`, `wsl/herdr-run`, `wsl/host_disk.py`, `wsl/jail.sh`, `wsl/land.py`, `wsl/landing_queue.py`, `wsl/orchestrator-policy.json`, `wsl/orchestrators.py`, `wsl/prio.py`, `wsl/rehome-worktree.sh`, `wsl/sandbox-defaults.py`, `wsl/suggest-worker-rules.json`, `wsl/suggest-worker.py`, `wsl/tier-backend.py`, `wsl/warden_heartbeat.py`, `wsl/wsl-worktree.sh`, `wsl/dotnet-bin/`
- `references/access-and-platforms.md`, `references/channels.md`, `references/cli.md`, `references/gate-scope.motif.json`, `references/landing.md`, `references/model-splits.md`
- `tests/test_admission.py`, `tests/test_channel_cli.py`, `tests/test_channels.py`, `tests/test_coord_slots.py`, `tests/test_fleet.py`, `tests/test_gate_priority.py`, `tests/test_landing.py`, `tests/test_landing_queue.py`, `tests/test_receipt_retries.py`, `tests/test_sandbox_defaults.py`, `tests/test_small_batches.py`, `tests/test_suggest_worker.py`, `tests/test_tier_backend.py`, `tests/test_orchestrators.py`, `tests/test_jail_roots.py`, `tests/test_dotnet_lanes.py`, `tests/test_lifecycle.py`, `tests/fixtures/`, `tests/run.py`, `tests/__init__.py`
- Then `README.md`, `SKILL.md`, the folder and the deployed `~/.claude/skills/herdr` link, with anything the owner has added since this list was read.

- [ ] **Step 1:** The owner names the group and confirms its condition holds. Stop here without that.
- [ ] **Step 2:** Re-derive the imports of what remains (`grep -nE "^(from|import) " wsl/*.py`, plus `spec_from_file_location`), because this list was read on 2026-10-31. Move any file still imported to Group D.
- [ ] **Step 3:** Delete the group's paths in `H:\repos\my-skills`. `tests/test_orchestrators.py`, `tests/test_jail_roots.py`, `tests/test_dotnet_lanes.py` and `tests/test_lifecycle.py` reference launch files, so trim or delete the affected cases.
- [ ] **Step 4:** Run `python tests/run.py` in the skill; expect OK for what remains.
- [ ] **Step 5:** Commit in that repo only after the owner reads the diff. No push.

---

## Gaps

- Everything Herdr-facing is built from the skill's docs and code, with no real pane touched. Task 15 Step 5 is the first real evidence, including whether the scheduled-task hub can reach the owner's Herdr server.
- Claude's idle read knows only the footers in `send-prompt.py`. Any other permission-mode footer reads as unknown, so that Director gets mail but no typed wake until a fixture is added.
- The Codex `N% context left` footer reading against a pinned window is unverified on a real session, and Claude's `PermissionRequest` hook is assumed to fire in a WSL pane.
- Stop-and-resume of a Codex session across `wsl --shutdown` is unverified. The spec expects wrong seat or side to explain most failures, which is why both are recorded.
- The rows in Task 16 Group D (gates, landing, channels, jail, .NET helpers) have no cutover named in the spec.

## Rulings

1. **The hub runs the compaction stop and resume, not the job.** The elevated job cannot open the ledger, its access to the owner's Herdr server is unverified, and the hub already winds down its own runs this way. `pane_stop(runtime)` waits on the hub's answer in `compaction-panes.json`. If the hub is down, the job goes on after 2 minutes and `runtime.kill` still clears WSL.
2. **Forced at 300k never interrupts.** The compaction stop is the single exception, so a working or focused pane gets no input at 300k. Forced means Drover stops waiting for the director's file, writes its own handoff from the ledger and checkpoint, and rotates at the first safe moment. Until then the owner sees an Inbox notice, and native compaction is pinned at 350k so it cannot race.
3. **"stop now" is skipped on a dialog or draft, and so is the interrupt.** Enter could confirm a selected option or submit the owner's text, and owner-only dialogs stay owner-only. The pane goes to the kill step, so such a pane can lose a draft overnight.
4. **Rotation types nothing into the old pane.** The new session's credential fences the old one, and the old pane closes through the 60-minute rules. No `/exit` is sent.
5. **A resident director declares `enforced_token_cap=False`.** An interactive CLI cannot enforce a spend cap, so `DirectorRuntime` drops that requirement for `resident` adapters only, and such launches reserve 0 tokens. Seat allowance and the context ceiling are the limits.
6. **`director_execution_enabled` stays false by default.** ADR-0012's finite default stands, and a Director in a pane is the owner's opt-in.
7. **Only rows with `started_by='drover'` get input.** An owner-started director uses the CLI and HTTP, and gets durable mail and no typed wake. If it runs in WSL, the kill step treats it as an unaccounted process.
8. **A pane run may write.** The old `sandbox='read-only'` director spec assumed a finite invocation, but a director may carry out yanked work. Resident runs use the seat's own sandbox and permission settings, never a bypass. Owner dialogs therefore appear and stay with the owner.
9. **`models.director` is the Claude default; a Codex director seat must set `director.model`.** Nothing is substituted. The default route follows the director seat, so planner runs move with it. The 25% reserve follows the director seat on any backend.
10. **The trust rule reads the seat's `CODEX_HOME/config.toml`.** Worktrees map to their main checkout by git common dir. Any read error is untrusted. The rule applies to a fresh pane at startup only. The skill's update-skip keys are not ported.
11. **Claude's own folder-trust dialog stays owner-only.** A Claude Director starting in a new worktree stops there with one notice. Point `director_execution.cwd` at a folder that seat already trusts.
12. **The status hook is Python, not bash.** One unittest covers both platforms, and the bash version already spawned `python3`. Stdlib only, runnable by path.
13. **The hook is installed by the owner.** Claude gets it per launch through `--settings`. Codex needs it in `CODEX_HOME`, which is a machine change and so a rollout step.
14. **The credential travels by private file only.** Windows uses an ACL. WSL uses a `0600` file on ext4, because the CLI refuses a drvfs file. A launcher file carries the environment so `pane run` shows only a path.
15. **"Side" is `placement` in code and text.** The spec says side; the glossary's term is Placement, and the glossary wins.
16. **"Drover management" means the owner's panes.** The hub stays a scheduled task and is never started in a pane, or Drover could not work with Herdr closed.
17. **Herdr closed means `unknown`, never `gone`.** A director held by that spends no replacement budget. `gone` needs Herdr up and the pane missing or its terminal changed.
18. **`rotated`, `pane-gone` and `compaction-fresh` are clean outcomes,** so they do not use up the single automatic replacement.
19. **Resident sessions renew in place.** `issue_session` caps at 24 hours and reissuing bumps the generation, so a new `renew_session` extends the same credential.
20. **Director context comes from different sources.** Claude uses the transcript, and Codex uses the footer's percent against the pinned 350k window. `[watch] context_tokens` stays the one 300k key; the only new key is `handoff_tokens`.
21. **`applies_to` limits are 6 KiB per file and 12 KiB in total.** The list parses as JSON with no YAML dependency. Whole files only; narrowest glob first, then newest ADR. All 14 current ADRs fit the file limit; at most about 4 of the 11 accepted ones that match `drover/**` fit the total. "No general overview" excludes the glossary, README, design docs and anything but accepted ADRs. Directors have no file scope, so they load none.
22. **Wake windows are role policy, not config,** beside `wake_limit_per_hour`. Urgent means an unacknowledged `critical` delivery, default window 60 seconds.
23. **Skill reaping does not carry over.** Drover closes only its own `retired` panes. The skill's exception for other programs' finished panes is gone.
24. **`queued_wedged` sends no key.** The skill allowed one submit; Drover publishes a notice and leaves it to the director or owner.
25. **The `model_drift` scan becomes one check after the first turn.** A mismatch is a notice, never a restart.
26. **New tabs, not splits.** The hub has no calling pane, so each agent gets a `--no-focus` tab in the `[panes] workspace`.
27. **All pane notices use `kind='pane.notice'`** through Phase 2's `disk_notices.publish`, so each is published once.
