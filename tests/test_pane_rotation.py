"""A rotation: the old CLI leaves by one guarded `/exit`, and only then does a fresh session take the pane. Fakes only."""
from unittest import mock

from drover import compaction_hold, handoff, pane_handoff, pane_launch, pane_send
from drover.ledger import ops_panes
from drover.pane_send import PaneRefused
from tests.pane_launch_support import CLAUDE_WORKING
from tests.pane_rotation_support import FORCED, HANDOFF, Rotating
from tests.pane_send_support import CLAUDE_IDLE, PANE
from tests.panes_support import ROLE


class OldCliLeavesFirst(Rotating):
    def test_valid_file_rotates_at_the_next_idle_moment(self):
        self.at()
        self.written()
        self.fake.agents[PANE].update(agent_status='working')
        self.fake.screens[PANE] = CLAUDE_WORKING
        self.assertEqual(self.at()['state'], 'live')
        self.assertEqual(self.exits(), [])
        self.idle()
        self.assertEqual(self.at()['state'], 'rotating')
        self.assertEqual(len(self.exits()), 1)
        self.cli_exits()
        self.follow()
        self.assertEqual(self.outcome(), 'rotated')
        self.assertEqual(self.row_now()['handoff_state'], 'rotated')

    def test_at_300k_drover_writes_its_own_handoff_and_rotates_when_safe(self):
        self.assertEqual(self.at(FORCED)['state'], 'rotating')
        self.assertEqual(handoff.missing(self.path.read_text(encoding='utf-8')), [])
        self.assertEqual(len(self.exits()), 1)
        self.cli_exits()
        self.follow()
        self.assertEqual(self.outcome(), 'rotated')

    def test_nothing_is_fenced_and_nothing_starts_while_the_old_cli_lives(self):
        self.told_to_exit()
        for _ in range(3):
            self.follow()
            self.assertEqual(self.tick()['launched'], [])
        self.assertIsNone(self.outcome())
        self.assertEqual(self.sessions(), 1)
        self.assertEqual(self.row_now()['state'], 'rotating')
        self.assertEqual(self.fake.seen.get(('tab', 'create')), 1)

    def test_a_read_that_cannot_say_ends_nothing(self):
        self.told_to_exit()
        self.fake.closed = True
        self.follow()
        self.assertIsNone(self.outcome())

    def test_the_exit_goes_once(self):
        self.told_to_exit()
        for _ in range(3):
            self.at()
            self.follow()
        self.tick()
        self.assertEqual(len(self.exits()), 1)

    def test_a_late_exit_is_one_notice_and_no_more_input(self):
        self.told_to_exit()
        self.fake.calls.clear()
        self.follow()
        self.assertEqual(self.notices('pane-exit:'), [])
        self.time += pane_handoff.EXIT_S + 1
        self.follow()
        self.follow()
        self.assertEqual(len(self.notices('pane-exit:')), 1)
        self.assertEqual(self.exits(), [])

    def test_a_refused_exit_waits_with_one_notice(self):
        self.at()
        self.written()
        with mock.patch.object(pane_send, 'exit_agent', side_effect=PaneRefused('last-write-unverified')):
            for _ in range(2):
                self.assertEqual(self.at()['state'], 'live')
        self.assertEqual(len(self.notices('pane-rotation-waits:')), 1)
        self.assertIsNone(self.outcome())
        self.assertIsNone(handoff.pending(self.port, ROLE))
        self.assertEqual(self.at()['state'], 'rotating')

    def died_before_the_write(self):
        self.at()
        self.written()
        with mock.patch.object(pane_send, 'exit_agent', side_effect=RuntimeError('the hub went away')):
            with self.assertRaises(RuntimeError):
                self.at()

    def test_rotating_and_the_pending_file_are_committed_before_the_exit(self):
        seen = []
        self.at()
        self.written()
        real = pane_send.exit_agent

        def noted(*args, **kw):
            seen.append((self.row_now()['state'], handoff.pending(self.port, ROLE)))
            return real(*args, **kw)
        with mock.patch.object(pane_send, 'exit_agent', noted):
            self.at()
        self.assertEqual(seen, [('rotating', str(self.path))])

    def test_a_hub_that_died_with_the_cli_gone_carries_on(self):
        self.died_before_the_write()
        self.cli_exits()
        self.follow()
        self.assertEqual(self.outcome(), 'rotated')

    def test_a_hub_that_died_before_the_write_rolls_back_and_exits_once(self):
        self.died_before_the_write()
        self.assertEqual(self.exits(), [])
        self.follow()
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertIsNone(handoff.pending(self.port, ROLE))
        self.assertIsNone(self.outcome())
        self.assertEqual(self.at()['state'], 'rotating')
        self.assertEqual(len(self.exits()), 1)

    def test_a_write_that_may_have_landed_is_never_made_again(self):
        self.told_to_exit()
        for state in ('sent', 'pending', 'unverified'):
            with self.subTest(state):
                self.conn.execute('UPDATE pane_writes SET state=? WHERE agent_id=? AND kind=?', (state, self.row['id'], 'exit'))
                self.follow()
                self.assertEqual(self.row_now()['state'], 'rotating')
        self.assertEqual(len(self.exits()), 1)

    def test_an_exit_that_never_went_out_rolls_back(self):
        self.died_before_the_write()
        unsent = ops_panes.note_write(self.conn, self.row['id'], 'exit', pane_send.EXIT_TEXT)
        ops_panes.finish_write(self.conn, unsent['id'], 'unsent', 'screen-changed')
        self.follow()
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_read_that_cannot_say_rolls_nothing_back(self):
        self.died_before_the_write()
        self.fake.closed = True
        self.follow()
        self.assertEqual(self.row_now()['state'], 'rotating')

    def test_an_empty_foreground_list_is_no_proof_the_cli_left(self):
        self.told_to_exit()
        self.fake.processes = dict(shell_pid=1, foreground_processes=[])
        self.follow()
        self.assertIsNone(self.outcome())
        self.assertEqual(self.sessions(), 1)

    def test_forced_closes_an_open_wake_when_the_read_proves_the_pane_idle(self):
        wake = ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        ops_panes.finish_write(self.conn, wake['id'], 'unverified', 'herdr-unavailable')
        self.assertEqual(self.at(FORCED)['state'], 'rotating')
        closed = self.conn.execute('SELECT state,reason FROM pane_writes WHERE id=?', (wake['id'],)).fetchone()
        self.assertEqual(tuple(closed), ('observed', 'superseded'))
        self.assertEqual(len(self.exits()), 1)

    def test_forced_with_a_turn_on_screen_closes_nothing(self):
        wake = ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        self.fake.screens[PANE] = CLAUDE_WORKING
        self.assertEqual(self.at(FORCED)['state'], 'live')
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id'])[0]['id'], wake['id'])

    def test_forced_with_a_screen_that_moves_closes_nothing_and_waits_with_one_notice(self):
        wake = ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        self.fake.screens[PANE] = [CLAUDE_IDLE, CLAUDE_IDLE, CLAUDE_IDLE + ' ', CLAUDE_IDLE + '  ']
        self.assertEqual(self.at(FORCED)['state'], 'live')
        self.assertEqual(self.exits(), [])
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id'])[0]['id'], wake['id'])
        self.assertEqual(len(self.notices('pane-rotation-waits:')), 1)

    def test_forced_never_passes_a_write_of_another_kind(self):
        ops_panes.note_write(self.conn, self.row['id'], 'brief', 'x')
        self.assertEqual(self.at(FORCED)['state'], 'live')
        self.assertEqual(self.exits(), [])
        self.assertEqual(len(self.notices('pane-rotation-waits:')), 1)

    def test_forced_with_a_wake_beside_another_kind_closes_neither(self):
        wake = ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        brief = ops_panes.note_write(self.conn, self.row['id'], 'brief', 'x')
        self.assertEqual(self.at(FORCED)['state'], 'live')
        self.assertEqual(self.exits(), [])
        self.assertEqual([write['id'] for write in ops_panes.open_writes(self.conn, self.row['id'])], [wake['id'], brief['id']])

    def test_under_the_forced_level_an_open_wake_still_waits(self):
        self.at()
        self.written()
        wake = ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        self.assertEqual(self.at(HANDOFF + 1)['state'], 'live')
        self.assertEqual(self.exits(), [])
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id'])[0]['id'], wake['id'])

    def test_a_handoff_request_left_open_does_not_stop_the_forced_exit(self):
        self.fake.hang = {('agent', 'prompt')}
        self.at()
        self.fake.hang = False
        self.assertEqual(self.at(FORCED)['state'], 'rotating')

    def test_no_exit_during_a_compaction(self):
        self.at()
        self.written()
        with mock.patch.object(compaction_hold, 'active', return_value={'id': 'h'}):
            self.assertEqual(self.at()['state'], 'live')
        self.assertEqual(self.exits(), [])

    def test_a_stop_during_a_rotation_still_ends_the_run_when_the_cli_leaves(self):
        self.told_to_exit()
        self.runtime.stop_active(self.notifications.admission()['epoch'])
        run = self.runtime.active_runs()[0]
        self.assertIsNotNone(run['stop_action_id'])
        self.cli_exits()
        self.panes._keep(run, self.guard)
        self.assertEqual(self.outcome(), 'rotated')
        state = self.conn.execute('SELECT state FROM action_outbox WHERE action_id=?', (run['stop_action_id'],)).fetchone()[0]
        self.assertEqual(state, 'complete')

    def test_a_row_not_rotating_is_never_followed(self):
        self.cli_exits()
        self.follow()
        self.assertIsNone(self.outcome())

    def test_a_rotating_agent_leaving_is_no_lost_director(self):
        self.told_to_exit()
        self.cli_exits()
        self.tick()
        self.assertEqual(self.notices('pane-agent-left:'), [])
        self.assertEqual(self.outcome(), 'rotated')


class Rotated(Rotating):
    def test_rotation_needs_no_replacement_budget(self):
        self.relaunched()
        self.assertEqual(self.outcome(), 'rotated')
        self.assertEqual(self.outcome(self.next_run), None)
        self.assertEqual(self.role()['replacements_used'], 0)

    def test_old_session_is_fenced_and_its_pane_retired(self):
        self.rotated()
        row = self.row_now()
        self.assertEqual((row['state'], row['handoff_state']), ('retired', 'rotated'))
        self.assertEqual(self.sessions(), 0)
        self.assertEqual(self.role()['pending_wake'], 1)
        event = self.port.events(kind='director.rotated')[-1]
        self.assertEqual(event['data']['run_id'], self.rid)
        self.assertEqual(self.role()['wake_position'], event['id'])

    def test_open_writes_close_with_the_row(self):
        self.told_to_exit()
        ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        self.cli_exits()
        self.follow()
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id']), [])

    def test_a_cleared_pending_wake_does_not_lose_the_role(self):
        self.rotated()
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        self.next_agent_boots()
        self.assertEqual(len(self.tick()['launched']), 1)

    def end_the_run(self, run_id):
        """A clean end with no mail left, the window passed, and a launch that needs no pane."""
        self.runtime.finish(run_id, dict(status='pane-gone', usage={}))
        for row in ops_panes.open_agents(self.conn):
            ops_panes.update_agent(self.conn, row['id'], state='gone')
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        self.time += 700
        with mock.patch.object(pane_launch, 'start'):
            return self.tick()['launched']

    def test_a_clean_end_gets_one_start_with_no_wake_and_then_waits_for_one(self):
        self.relaunched()
        again, = self.end_the_run(self.next_run)
        self.assertEqual(self.runtime.active_runs()[0]['spec']['handoff'], str(self.path))
        self.assertEqual(self.end_the_run(again), [])
        self.assertEqual(self.tick()['launched'], [])
        self.assertEqual(handoff.pending(self.port, ROLE), str(self.path))
        self.wake()
        with mock.patch.object(pane_launch, 'start'):
            self.assertEqual(len(self.tick()['launched']), 1)
        self.assertEqual(self.runtime.active_runs()[0]['spec']['handoff'], str(self.path))

    def test_a_launch_held_before_any_agent_ran_is_no_attempt(self):
        self.rotated()
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        with mock.patch.object(pane_launch, 'rotate', side_effect=pane_launch.AdmissionHeld('pane-not-rotated')), \
                mock.patch.object(pane_launch, 'start', side_effect=pane_launch.AdmissionHeld('held')):
            for _ in range(3):
                self.time += 700
                self.tick()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs WHERE outcome='admission-held'").fetchone()[0], 3)
        self.assertTrue(handoff.owed(self.port, ROLE, ('admission-held', 'pane-gone', 'rotated')))
        self.time += 700
        self.next_agent_boots()
        self.assertEqual(len(self.tick()['launched']), 1)

    def test_a_failed_end_is_still_recovery(self):
        self.relaunched()
        again, = self.end_the_run(self.next_run)
        self.conn.execute("UPDATE runs SET outcome='launch-failed' WHERE id=?", (again,))
        self.assertTrue(handoff.owed(self.port, ROLE, ('pane-gone', 'rotated')))

    def test_nothing_is_owed_with_no_pending_file(self):
        self.relaunched()
        handoff.set_pending(self.port, ROLE, None)
        self.assertFalse(handoff.owed(self.port, ROLE, ('pane-gone', 'rotated')))

    def test_last_rotations_restatement_does_not_count(self):
        self.told_to_exit()
        self.restated.write_text('from before', encoding='utf-8')
        self.cli_exits()
        self.follow()
        self.assertFalse(self.restated.exists())

    def test_a_restatement_that_cannot_be_cleared_ends_nothing(self):
        self.told_to_exit()
        self.cli_exits()
        with mock.patch.object(handoff, 'clear', side_effect=PermissionError('held')):
            self.follow()
        self.assertIsNone(self.outcome())
