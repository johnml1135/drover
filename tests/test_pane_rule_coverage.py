"""Every one of the 32 pane safety rules names Drover tests that exist."""
import unittest


RULES = {
    1: (
        "tests.test_herdr_client.Isolation.test_disabled_or_missing_binary_makes_no_call",
    ),
    2: (
        "tests.test_pane_send.Check.test_pane_without_a_drover_row_gets_mail_not_input",
        "tests.test_pane_close.Candidates.test_other_panes_are_never_closed",
    ),
    3: (
        "tests.test_pane_send.Check.test_busy_blocked_unknown_and_draft_screens_send_nothing",
        "tests.test_pane_send.Check.test_owner_draft_on_last_read_sends_nothing",
        "tests.test_pane_screen.Composer.test_collapsed_paste_is_pending_not_ours",
    ),
    4: (
        "tests.test_pane_screen.Viewport.test_copy_viewport_is_held_not_working",
        "tests.test_pane_screen.Viewport.test_viewport_marker_in_transcript_is_not_held",
        "tests.test_pane_send.Check.test_scrolled_or_unknown_viewport_sends_nothing",
        "tests.test_pane_send.Check.test_viewport_is_checked_again_just_before_send",
    ),
    5: (
        "tests.test_pane_wake.Verification.test_wake_counts_only_with_turn_activity_and_a_new_checkpoint",
        "tests.test_pane_wake.Verification.test_unverified_wake_is_not_replayed_after_hub_restart",
    ),
    6: (
        "tests.test_pane_send.Send.test_stalled_send_is_read_not_resent",
        "tests.test_pane_launch.FirstTurn.test_brief_is_submitted_once_and_never_retried",
        "tests.test_pane_launch.FirstTurn.test_empty_composer_after_send_proves_nothing",
        "tests.test_pane_launch.FirstTurn.test_failed_submission_is_not_live",
    ),
    7: (
        "tests.test_pane_screen.Working.test_visible_working_overrides_stale_idle",
        "tests.test_pane_screen.Working.test_narrow_pane_working_row",
    ),
    8: (
        "tests.test_pane_screen.Queue.test_queued_wake_is_recognised_only_for_its_scope",
        "tests.test_pane_wake.Overdue.test_queue_idle_ten_minutes_is_wedged_and_sends_no_key",
    ),
    9: (
        "tests.test_pane_launch.Trust.test_codex_trust_is_answered_for_a_worktree_of_a_trusted_repo",
        "tests.test_pane_launch.Trust.test_codex_trust_is_left_for_an_untrusted_repo",
        "tests.test_pane_launch.Trust.test_trust_unreadable_config_is_owner_only",
        "tests.test_pane_launch.Trust.test_claude_trust_dialog_stays_owner_only",
    ),
    10: (
        "tests.test_pane_guard.Startup.test_startup_keys_answers_only_codex_trust_in_a_fresh_pane",
        "tests.test_pane_launch.Reused.test_reused_pane_inherits_no_consent",
    ),
    11: (
        "tests.test_pane_guard.ModelDialog.test_model_dialog_blocks_without_a_choice",
        "tests.test_pane_guard.OtherDialogs.test_selection_approval_question_trust_and_numbered_options_block",
        "tests.test_pane_guard.OtherDialogs.test_strong_modal_overrides_working",
        "tests.test_pane_guard.OtherDialogs.test_selected_option_is_not_a_composer",
        "tests.test_pane_guard.OtherDialogs.test_words_in_working_scrollback_do_not_block",
        "tests.test_pane_send.Send.test_dialog_after_paste_stops_the_enter",
    ),
    12: (
        "tests.test_pane_screen.ClaudeIdle.test_claude_uses_bottom_region_not_scrollback",
        "tests.test_pane_screen.ClaudeIdle.test_unknown_claude_layout_fails_closed",
    ),
    13: (
        "tests.test_pane_send.Check.test_requires_row_unfocused_idle_empty_and_unchanged",
        "tests.test_pane_send.Check.test_occupant_change_between_reads_sends_nothing",
        "tests.test_pane_send.Check.test_no_option_clears_a_dialog",
    ),
    14: (
        "tests.test_pane_send.Send.test_wsl_agent_gets_one_bracketed_paste_then_enter",
    ),
    15: (
        "tests.test_pane_wake.Text.test_wake_text_has_no_digit_and_grants_nothing",
    ),
    16: (
        "tests.test_pane_wake.Text.test_wake_carries_no_message_body",
        "tests.test_pane_wake.Text.test_mail_stays_pending_until_the_director_commits",
    ),
    17: (
        "tests.test_pane_screen.Composer.test_own_prompt_needs_the_full_digest",
        "tests.test_pane_screen.Composer.test_prefix_of_our_prompt_is_a_draft",
        "tests.test_pane_send.SubmitOwn.test_unsent_own_prompt_gets_one_enter",
    ),
    18: (
        "tests.test_pane_send.Interrupt.test_interrupt_is_one_ctrl_c_to_an_own_pane",
        "tests.test_pane_close.Closing.test_pane_code_has_no_pattern_kill",
    ),
    19: (
        "tests.test_pane_close.Candidates.test_only_retired_rows_are_candidates",
        "tests.test_pane_close.Clock.test_an_hour_unchanged_is_the_candidate",
        "tests.test_pane_close.Clock.test_any_change_restarts_the_clock",
        "tests.test_pane_close.Gates.test_a_draft_or_an_open_dialog_keeps_the_pane",
        "tests.test_pane_close.Gates.test_uncommitted_or_untracked_changes_keep_the_pane",
        "tests.test_pane_close.Gates.test_a_live_process_outside_the_pane_keeps_the_checkout",
        "tests.test_pane_close.Gates.test_a_change_just_before_close_keeps_the_pane",
        "tests.test_pane_close.Closing.test_dry_run_is_the_default",
        "tests.test_pane_close.Closing.test_apply_closes_by_pane_id_and_logs_evidence_and_reason",
    ),
    20: (
        "tests.test_pane_close.Candidates.test_live_director_pane_is_never_closed",
    ),
    21: (
        "tests.test_panes.Rules.test_second_pane_role_is_held_multi_director_parked",
        "tests.test_panes.Rules.test_stale_generation_gets_no_pane_write",
        "tests.test_panes.Rules.test_lease_grants_no_pane_write",
    ),
    22: (
        "tests.test_panes.Rules.test_changed_occupant_stops_wakes_and_keeps_the_pane",
    ),
    23: (
        "tests.test_panes.Rules.test_herdr_restart_renames_by_pane_and_cwd",
    ),
    24: (
        "tests.test_herdr_client.Allowlist.test_bare_and_server_commands_are_refused",
        "tests.test_herdr_client.Isolation.test_tests_cannot_reach_a_real_server",
    ),
    25: (
        "tests.test_pane_launch.Opening.test_every_pane_opens_without_focus",
        "tests.test_pane_send.Send.test_targets_are_explicit_pane_ids",
    ),
    26: (
        "tests.test_pane_launch.Startup.test_brief_waits_for_an_idle_composer_and_never_types_into_a_shell",
        "tests.test_pane_launch.Startup.test_agent_that_exited_after_self_update_is_refused",
    ),
    27: (
        "tests.test_pane_send.Send.test_prompts_never_use_wait",
        "tests.test_pane_send.Check.test_busy_agent_gets_no_follow_up",
    ),
    28: (
        "tests.test_pane_launch.FirstTurn.test_footer_mismatch_after_first_turn_goes_to_the_inbox",
        "tests.test_config.DirectorSeat.test_any_configured_seat_needs_no_model_minimum",
    ),
    29: (
        "tests.test_pane_launch.Holds.test_missing_cwd_fails_before_a_pane_opens",
    ),
    30: (
        "tests.test_panes.Rules.test_one_launch_at_a_time_and_a_stuck_launch_leaves_the_tick_running",
    ),
    31: (
        "tests.test_panes.Rules.test_only_the_hub_constructs_a_herdr_client",
    ),
    32: (
        "tests.test_pane_send.Check.test_only_an_unfocused_idle_empty_composer_is_written",
    ),
}


class EveryRuleHasATest(unittest.TestCase):
    def test_there_are_32_rules(self):
        self.assertEqual(sorted(RULES), list(range(1, 33)))

    def test_every_rule_names_a_test_that_exists(self):
        loader = unittest.TestLoader()
        for rule, names in RULES.items():
            self.assertTrue(names, "rule %d names no test" % rule)
            for name in names:
                with self.subTest(rule=rule, test=name):
                    suite = loader.loadTestsFromName(name)
                    self.assertEqual(suite.countTestCases(), 1)
                    self.assertNotIn("_FailedTest", repr(suite))


if __name__ == "__main__":
    unittest.main()
