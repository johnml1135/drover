"""A resident director across ticks: session renewal, an unreachable Herdr, and upkeep that never reaches the tick."""
import unittest
from unittest import mock

from drover import compaction_hold, config, pane_close, pane_files, pane_handoff, pane_launch, pane_wake, panes
from drover.directors import DirectorRuntime
from drover.ledger import ops_panes
from drover.notifications import roles
from tests.pane_launch_support import LONE_SHELL, SECRET
from tests.pane_send_support import CLAUDE_IDLE, PANE
from tests.panes_support import ROLE, Resident


class Sessions(Resident):
    def expiry(self):
        return self.conn.execute('SELECT expires FROM director_sessions WHERE role_id=?', (ROLE,)).fetchone()[0]

    def test_session_renews_before_expiry_without_a_new_generation(self):
        self.launched()
        first, generation = self.expiry(), self.role()['generation']
        self.assertAlmostEqual(first - self.time, 86400, delta=1)
        self.time += 40000
        self.tick()
        self.assertEqual(self.expiry(), first, 'more than half the session is left')
        for _ in range(3):
            self.time += 40000
            self.tick()
        self.assertGreater(self.time, first)
        self.assertEqual(self.role()['generation'], generation)
        self.assertEqual([r['run_id'] for r in self.runtime.active_runs()], [self.rid])
        credential = next(text for path, text, _ in self.private)
        self.assertEqual(self.notifications.authenticate(credential)['generation'], generation)

    def test_a_session_renews_while_herdr_is_closed(self):
        self.launched()
        first = self.expiry()
        self.fake.closed = True
        self.time += 50000
        self.tick()
        self.assertGreater(self.expiry(), first)

    def test_a_run_being_stopped_is_not_renewed(self):
        self.launched()
        first = self.expiry()
        self.time += 50000
        self.panes.maintain(dict(self.run_row, stop_action_id=1), self.guard)
        self.assertEqual(self.expiry(), first)

    def test_a_changed_occupant_is_not_renewed(self):
        self.launched()
        first = self.expiry()
        self.fake.agents[PANE]['agent_session'] = {'value': 'someone-else'}
        self.time += 50000
        self.panes.maintain(self.run_row, self.guard)
        self.assertEqual(self.expiry(), first)

    def test_a_stale_generation_is_not_renewed(self):
        self.launched()
        self.notifications.issue_session(ROLE, 'owner-replacement')
        first = self.expiry()
        self.time += 50000
        self.panes.maintain(self.run_row, self.guard)
        self.assertEqual(self.expiry(), first)


    def test_an_agent_that_left_is_not_renewed(self):
        self.launched()
        first = self.expiry()
        del self.fake.agents[PANE]
        self.fake.processes = LONE_SHELL
        self.time += 50000
        self.panes.maintain(self.run_row, self.guard)
        self.assertEqual(self.expiry(), first)

    def test_a_failed_row_is_not_renewed(self):
        self.launched()
        first = self.expiry()
        ops_panes.update_agent(self.conn, self.row['id'], state='failed')
        self.time += 50000
        self.panes.maintain(self.run_row, self.guard)
        self.assertEqual(self.expiry(), first)

    def test_renewing_needs_the_current_session(self):
        self.launched()
        session = self.role()['session_id']
        with self.assertRaises(PermissionError):
            roles.renew_session(self.notifications, ROLE, 'another-session')
        self.time = self.expiry() + 1
        with self.assertRaises(PermissionError):
            roles.renew_session(self.notifications, ROLE, session)

    def test_renewing_never_shortens_a_session(self):
        self.launched()
        first = self.expiry()
        renewed = roles.renew_session(self.notifications, ROLE, self.role()['session_id'], ttl=60)
        self.assertEqual(renewed['expires_at'], first)
        self.assertEqual(self.expiry(), first)
        with self.assertRaises(ValueError):
            roles.renew_session(self.notifications, ROLE, self.role()['session_id'], ttl=86401)


class HerdrClosed(Resident):
    def owner_question_status(self):
        notice = next(e for e in self.port.events(kind='pane.notice') if e['data']['notice']['id'].startswith('pane-herdr-closed:'))
        return self.conn.execute('SELECT status FROM questions WHERE id=?', (notice['data']['question'],)).fetchone()[0]

    def test_herdr_closed_does_not_spend_the_replacement(self):
        self.launched()
        generation = self.role()['generation']
        self.fake.closed = True
        for _ in range(3):
            self.time += 30
            self.wake()
            self.assertEqual(self.tick()['launched'], [])
        self.assertEqual(self.role()['replacements_used'], 0)
        self.assertEqual(self.role()['generation'], generation)
        self.assertEqual([r['run_id'] for r in self.runtime.active_runs()], [self.rid])
        self.assertEqual(self.outcome(), None)

    def test_herdr_closed_is_one_owner_message_and_clears_when_it_returns(self):
        self.launched()
        self.fake.closed = True
        for _ in range(3):
            self.time += 1      # a later clock must not read as a later outage
            self.tick()
        self.assertEqual(len([n for n in self.notices() if n['id'].startswith('pane-herdr-closed:')]), 1)
        self.assertEqual(self.open_questions(), 0, 'the notice is the one message')
        self.assertEqual(self.runtime.active_runs()[0]['held_reason'], 'consumer-liveness-uncertain')
        self.fake.closed = False
        self.tick()
        self.assertEqual(self.open_questions(), 0)
        self.assertIsNone(self.runtime.active_runs()[0]['held_reason'])
        self.assertEqual(self.owner_question_status(), 'answered')

    def test_a_good_launch_closes_the_outage(self):
        self.build()
        self.turns()
        self.fake.closed = True
        self.wake()
        self.tick()
        self.assertEqual(self.owner_question_status(), 'open')
        self.fake.closed = False
        self.time += 1
        self.assertEqual(len(self.tick()['launched']), 1)
        self.assertEqual(self.owner_question_status(), 'answered')

    def test_a_second_outage_is_a_second_notice(self):
        self.launched()
        for closed in (True, False, True):
            self.time += 1
            self.fake.closed = closed
            self.tick()
        self.assertEqual(len([n for n in self.notices() if n['id'].startswith('pane-herdr-closed:')]), 2)

    def test_a_launch_with_herdr_closed_starts_nothing_with_one_notice(self):
        self.build()
        self.fake.closed = True
        self.wake()
        self.tick()
        self.assertEqual(self.runtime.active_runs(), [])
        self.assertIsNone(self.role()['held_reason'])
        self.assertEqual(self.calls('tab', 'create'), [])
        self.assertEqual(len([n for n in self.notices() if n['id'].startswith('pane-herdr-closed:')]), 1)

    def test_a_restart_of_herdr_keeps_the_generation(self):
        self.launched()
        generation, session = self.role()['generation'], self.role()['session_id']
        self.fake.closed = True
        self.tick()
        self.fake.closed = False
        self.fake.restart()
        self.tick()
        self.assertEqual((self.role()['generation'], self.role()['session_id']), (generation, session))
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(len(self.calls('tab', 'create')), 1)
        self.assertIsNone(self.runtime.active_runs()[0]['held_reason'])

    def test_pane_gone_with_herdr_up_finishes_as_pane_gone(self):
        self.launched()
        del self.fake.panes[PANE]
        self.tick()
        self.assertEqual(self.outcome(), 'pane-gone')
        self.assertEqual(self.runtime.active_runs(), [])
        self.assertEqual(self.role()['replacements_used'], 0)
        self.assertEqual(self.row_now()['state'], 'gone')

    def test_a_hub_restart_finds_the_pane_gone_as_pane_gone(self):
        self.launched()
        del self.fake.panes[PANE]
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        self.tick()
        self.assertEqual(self.outcome(), 'pane-gone')

    def test_a_hub_restart_adopts_a_live_pane(self):
        self.launched()
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        self.assertEqual(self.tick()['recovery']['adopted'], [self.rid])
        self.assertEqual(len(self.calls('tab', 'create')), 1)


class Maintenance(Resident):
    def test_no_exception_leaves_maintain(self):
        self.launched()
        boom = mock.Mock(side_effect=OSError('secret text'))
        for module, name in ((pane_wake, 'deliver'), (pane_handoff, 'maintain'), (pane_close, 'tick')):
            with self.subTest(name), mock.patch.object(module, name, boom):
                self.panes.maintain(self.run_row, self.guard)
        with mock.patch.object(ops_panes, 'by_run', boom):
            self.panes.maintain(self.run_row, self.guard)
        with mock.patch.object(ops_panes, 'open_agents', boom):
            self.panes.maintain(self.run_row, self.guard)

    def test_one_failing_step_leaves_the_others(self):
        self.launched()
        wake, handoff = self.hooks()
        wake.side_effect = OSError('secret text')
        with self.assertLogs('drover.panes', 'WARNING') as logs:
            self.panes.maintain(self.run_row, self.guard)
        handoff.assert_called_once()
        self.assertNotIn('secret text', ''.join(logs.output))

    def test_a_row_in_compaction_gets_no_wake(self):
        self.launched()
        wake, handoff = self.hooks()
        ops_panes.update_agent(self.conn, self.row['id'], state='compaction')
        self.panes.maintain(self.run_row, self.guard)
        wake.assert_not_called()
        handoff.assert_not_called()

    def test_pane_close_stays_dry_unless_the_owner_enables_it(self):
        self.launched()
        with mock.patch.object(pane_close, 'tick', return_value=[]) as tick:
            self.panes.maintain(self.run_row, self.guard)
            self.port.set_setting('pane_close_enabled', 'true')
            self.panes.maintain(self.run_row, self.guard)
            self.port.set_setting('pane_close_enabled', 'no')
            self.panes.maintain(self.run_row, self.guard)
        self.assertEqual([c.kwargs['apply'] for c in tick.call_args_list], [False, True, False])

    def test_pane_close_runs_on_a_tick_with_no_active_run(self):
        self.build()
        with mock.patch.object(pane_close, 'tick', return_value=[]) as tick:
            self.tick()
        tick.assert_called_once()
        self.assertEqual(tick.call_args.args[:2], (self.port, self.herdr))
        self.assertEqual(tick.call_args.args[2], self.time)

    def test_nothing_is_closed_during_a_compaction_hold(self):
        self.launched()
        with mock.patch.object(compaction_hold, 'active', return_value=dict(id='h1')), \
                mock.patch.object(pane_close, 'tick', return_value=[]) as tick:
            self.panes.sweep()
        tick.assert_not_called()

    def test_pausing_dispatch_leaves_a_resident_pane_alone(self):
        self.launched()
        self.port.set_setting('dispatch_enabled', 'false')
        self.tick()
        self.assertEqual(self.calls('pane', 'send-keys'), [])
        self.assertIsNone(self.runtime.active_runs()[0]['stop_action_id'])
        self.port.set_setting('dispatch_enabled', 'true')
        self.tick()
        self.assertIsNone(self.runtime.active_runs()[0]['held_reason'])

    def test_the_owner_stop_still_reaches_a_resident_pane(self):
        self.launched()
        self.fake.screens[PANE], self.fake.agents[PANE]['agent_status'] = CLAUDE_IDLE, 'idle'
        self.notifications.stop('owner', 'stop')
        self.tick()
        self.assertEqual(self.calls('pane', 'send-keys'), [['pane', 'send-keys', PANE, 'ctrl+c']])

    def test_execution_stays_off_by_default(self):
        self.build()
        self.conn.execute("DELETE FROM settings WHERE key='director_execution_enabled'")
        self.conn.commit()
        self.wake()
        self.assertIn('execution-disabled', str(self.tick()['held']))
        self.assertEqual(self.calls('tab', 'create'), [])
        self.assertIsNone(self.role()['session_id'])

    def test_the_spec_takes_the_seat_sandbox_and_writes(self):
        self.build()
        self.turns()
        self.wake()
        with mock.patch.object(config, 'seat_by_name', return_value=dict(sandbox='workspace-write')), \
                mock.patch.object(pane_launch, 'start', return_value={}) as start:
            self.tick()
        spec = start.call_args.args[2]
        self.assertEqual((spec['sandbox'], spec['write']), ('workspace-write', True))
        self.assertNotIn('exit', spec['user_prompt'])
        self.assertIn('python -m drover.notifications inbox', spec['user_prompt'])

    def test_a_seat_that_cannot_be_read_stays_read_only(self):
        self.build()
        self.wake()
        with mock.patch.object(pane_launch, 'start', return_value={}) as start:
            self.tick()
        self.assertEqual(start.call_args.args[2]['sandbox'], 'read-only')

    def test_a_codex_seat_sandbox_reaches_its_flags_and_nothing_else_does(self):
        spec = dict(backend='codex', sandbox='workspace-write')
        args = pane_files.agent_args(spec, model='m', effort='high')
        self.assertEqual(args[args.index('-s'):args.index('-s') + 2], ['-s', 'workspace-write'])
        self.assertNotIn('-s', pane_files.agent_args(dict(backend='codex'), model='m', effort='high'))
        self.assertNotIn('-s', pane_files.agent_args(dict(backend='claude', sandbox='read-only'), model='m', effort='high'))
        with self.assertRaises(ValueError):
            pane_files.agent_args(dict(backend='codex', sandbox='danger-full-access'), model='m', effort='high')

    def test_credential_stays_out_of_the_ledger_and_logs(self):
        self.launched()
        self.fake.closed = True
        self.tick()
        rows = self.conn.execute('SELECT kind,data FROM task_events').fetchall()
        self.assertNotIn(SECRET, str([dict(r) for r in rows]))


class Sweep(Resident):
    def retired(self, placement, cwd):
        self.build()
        self.row = ops_panes.record_agent(self.conn, pane_id='w1:p9', terminal_id='t9', kind='claude', seat='seat-c',
                                          placement=placement, started_by='drover', cwd=cwd, state='retired')

    def closing(self, up):
        with mock.patch.object(panes, 'wsl_running', return_value=up) as running, \
                mock.patch.object(panes.pane_close, 'tick', return_value=[]) as tick:
            self.panes.sweep()
        return running, tick

    def test_a_wsl_checkout_is_not_read_while_wsl_is_down(self):
        self.retired('wsl', '/home/me/repo')
        self.closing(False)[1].assert_not_called()

    def test_it_is_read_while_wsl_is_up(self):
        self.retired('wsl', '/home/me/repo')
        self.closing(True)[1].assert_called_once()

    def test_it_goes_by_the_checkout_and_not_the_seat(self):
        self.retired('windows', '/home/me/repo')
        self.closing(False)[1].assert_not_called()

    def test_only_a_retired_row_is_read(self):
        self.retired('wsl', '/home/me/repo')
        ops_panes.update_agent(self.conn, self.row['id'], state='failed')
        running, tick = self.closing(False)
        running.assert_not_called()
        tick.assert_called_once()

    def test_a_windows_pane_never_asks_after_wsl(self):
        self.retired('windows', 'C:\\repos\\a')
        running, tick = self.closing(False)
        running.assert_not_called()
        tick.assert_called_once()


if __name__ == '__main__':
    unittest.main()
