"""The compaction stop reaches panes and agents resume. Herdr and the job's commands are always fakes."""
import unittest
from types import SimpleNamespace
from unittest import mock

from drover import compaction, compaction_hold, pane_compaction, pane_launch, pane_send
from drover.dispatcher import Dispatcher
from drover.launch import AdmissionHeld
from drover.ledger import ops_panes
from tests.compaction_fixture import NOW
from tests.dispatch_fixture import make_dispatcher
from tests.fakeci import InProcessLauncher
from tests.pane_compaction_support import CTRL_C, Overnight, PANE, ROLE, SESSION, WORKING, paste
from tests.pane_send_support import DIALOG, DRAFT, SUSPECT, composing

STOP = paste('stop now')
RESUME = paste('restart complete, keep going')


class Who(Overnight):
    def test_only_drover_wsl_rows_are_touched(self):
        self.up()
        owner = ops_panes.record_agent(self.conn, pane_id='w1:p8', terminal_id='t8', session_id='o', kind='codex', seat='seat-x',
                                       placement='wsl', started_by='owner', state='live')
        self.at(0)
        self.assertEqual([a['pane_id'] for a in self.answer['agents']], [PANE])
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p8')['state'], 'live')
        self.assertFalse([call for call in self.fake.calls if 'w1:p8' in call])
        self.assertEqual(owner['state'], 'live')

    def test_windows_claude_director_is_left_alone(self):
        self.up()
        ops_panes.record_agent(self.conn, pane_id='w1:p9', terminal_id='t9', session_id='w', kind='claude', seat='seat-c',
                               placement='windows', started_by='drover', state='live')
        for seconds in (0, 300, 360):
            self.at(seconds)
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p9')['state'], 'live')
        self.assertFalse([call for call in self.fake.calls if 'w1:p9' in call])

    def test_codex_director_is_stopped_as_a_wsl_pane_agent(self):
        row = self.up()
        self.at(0)
        self.assertEqual(row['kind'], 'codex')
        self.assertEqual(self.new_row()['state'], 'compaction')
        self.assertEqual(self.sends()[0], STOP)
        self.assertEqual((self.agent()['seat'], self.agent()['placement']), ('seat-x', 'wsl'))

    def test_retired_panes_are_not_resumed(self):
        self.up()
        ops_panes.update_agent(self.conn, self.row['id'], state='retired')
        self.at(0)
        self.comes_back()
        self.at(900)
        self.assertEqual(self.answer['agents'], [])
        self.assertTrue(self.answer['settled'])
        self.assertEqual(self.sends(), [])


class Stop(Overnight):
    def test_stop_now_goes_out_once_and_the_interrupt_after_300s(self):
        self.up()
        for seconds in (0, 100, 299):
            self.at(seconds)
        self.assertEqual(self.writes(), [STOP, ['pane', 'send-keys', PANE, 'enter']])
        self.at(300)
        self.at(330)
        self.assertEqual(self.writes()[2:], [CTRL_C])
        self.assertEqual(self.writes().count(STOP), 1)

    def test_an_agent_that_stops_when_asked_gets_no_interruption(self):
        self.up()
        self.fake.settles = True
        self.at(0)
        self.assertFalse(self.answer['settled'], 'the tick that sent it proves nothing')
        self.at(20)
        self.assertEqual(self.agent()['outcome'], 'stopped-asked')
        self.assertTrue(self.answer['settled'])
        self.at(300)
        self.assertNotIn(CTRL_C, self.sends())

    def test_an_agent_that_stops_on_the_interruption_says_so(self):
        self.up()
        self.at(0)
        self.at(300)
        self.settle()
        self.at(310)
        self.assertEqual(self.agent()['outcome'], 'stopped-interrupted')
        self.assertTrue(self.answer['settled'])

    def test_kill_is_reached_at_360s(self):
        self.up()
        for seconds in (0, 300, 359):
            self.at(seconds)
        self.assertFalse(self.answer['settled'])
        self.assertIsNone(self.agent()['outcome'])
        self.at(360)
        self.assertTrue(self.answer['settled'])
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('left-for-kill', 'still-working'))

    def test_dialog_or_draft_skips_text_and_interrupt_and_waits_for_the_kill(self):
        for screen, reason in ((DIALOG, 'dialog'), (DRAFT, 'draft'), (SUSPECT, 'suspected-dialog')):
            with self.subTest(reason):
                self.setUp()
                self.up()
                self.fake.screens[PANE] = screen
                self.at(0)
                self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('left-for-kill', reason))
                self.assertFalse(self.answer['settled'], 'it waits for its deadline')
                self.fake.screens[PANE] = WORKING       # whatever the pane does next, it has had its one chance
                self.at(100)
                self.at(300)
                self.assertFalse(self.answer['settled'])
                self.at(360)
                self.assertTrue(self.answer['settled'])
                self.assertEqual(self.sends(), [])
                self.assertEqual(self.agent()['outcome'], 'left-for-kill')

    def test_a_refused_interrupt_is_left_for_the_kill(self):
        self.up()
        self.at(0)
        self.fake.screens[PANE] = DIALOG
        self.at(300)
        self.assertEqual(self.agent()['outcome'], 'left-for-kill')
        self.assertNotIn(CTRL_C, self.sends())

    def test_an_unproved_stop_is_never_typed_again(self):
        self.up()
        self.sleep.side_effect = lambda seconds: self.fake.screens.update({PANE: composing('stop now and more')})
        self.at(0)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('left-for-kill', 'not-own'))
        self.at(300)
        self.assertEqual(self.writes(), [STOP])

    def test_timings_survive_a_hub_restart(self):
        self.up()
        self.at(0)
        self.hub_restart()
        self.at(200)
        self.assertEqual(self.writes().count(STOP), 1)
        self.assertNotIn(CTRL_C, self.sends())
        self.at(300)
        self.hub_restart()
        self.at(310)
        self.assertEqual(self.writes().count(CTRL_C), 1)

    def test_a_refusal_survives_a_hub_restart(self):
        self.up()
        self.fake.screens[PANE] = DRAFT
        self.at(0)
        self.fake.screens[PANE] = WORKING
        self.hub_restart()
        self.at(20)
        self.assertEqual(self.sends(), [])

    def test_a_row_claimed_before_a_crash_is_not_stopped_again(self):
        self.up()
        with mock.patch.object(pane_send, 'send_stop', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.at(0)                  # nothing catches this: the hub is gone between the claim and the send
        self.hub_restart()
        self.at(20)
        self.assertEqual(self.sends(), [])

    def test_session_id_is_in_the_answer_before_the_kill(self):
        self.up()
        self.at(0)
        self.assertEqual(self.saved()['agents'][0]['session_id'], SESSION)
        self.assertEqual(self.saved()['id'], 'h1')
        self.assertEqual(self.saved()['phase'], 'stop')

    def test_a_tick_after_the_job_moved_on_types_nothing(self):
        self.up()
        self.hold_ = dict(self.hold_, step='kill')
        self.at(10)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.agent()['outcome'], 'left-for-kill')
        self.assertTrue(self.answer['settled'])

    def test_compaction_kill_does_not_spend_the_replacement(self):
        self.up()
        self.at(0)
        self.killed()
        self.tick()
        self.assertTrue(self.outcome_is_open())
        self.comes_back(boots=False)
        self.at(900)
        self.assertEqual(self.outcome(), 'compaction-fresh')
        self.released()
        self.fake.calls.clear()
        with mock.patch.object(self.runtime.service, 'replace') as replace, mock.patch.object(pane_launch, 'start') as start:
            self.tick()
        replace.assert_not_called()
        start.assert_called_once()


class Resume(Overnight):
    def stopped(self):
        self.up()
        self.fake.settles = True
        self.at(0)
        self.fake.calls.clear()
        self.comes_back()

    def test_resume_sends_restart_complete_keep_going(self):
        self.stopped()
        self.at(900)
        self.assertEqual(self.writes()[-2], RESUME)
        self.assertEqual(self.agent()['outcome'], 'resumed')
        self.assertTrue(self.answer['settled'])
        self.assertEqual(self.saved()['phase'], 'resume')
        self.assertTrue(self.outcome_is_open())

    def test_resume_uses_the_recorded_seat_and_placement(self):
        self.stopped()
        self.at(900)
        row = self.new_row()
        self.assertNotEqual(row['id'], self.row['id'])
        self.assertEqual((row['seat'], row['placement'], row['kind'], row['session_id'], row['run_id'], row['pane_id']),
                         ('seat-x', 'wsl', 'codex', SESSION, self.rid, PANE))
        self.assertEqual(self.row_now()['state'], 'gone')
        script = (self.state / 'panes' / str(self.rid) / 'launch.sh').read_text(encoding='utf-8')
        self.assertIn(f'resume {SESSION}', ' '.join(script.replace("'", ' ').split()))
        self.assertEqual(self.calls('tab', 'create'), [], 'the same pane')

    def test_a_slow_agent_gets_the_text_on_a_later_tick(self):
        self.stopped()
        self.fake.after[('pane', 'run', 2)] = lambda: (self.boot(), self.fake.agents[PANE].update(agent_status='working'))
        self.at(900)
        self.assertIsNone(self.agent()['outcome'])
        self.assertFalse(self.answer['settled'])
        self.assertEqual(self.writes(), [self.writes()[0]], 'only the launch line')
        self.settle()
        self.at(920)
        self.assertEqual(self.agent()['outcome'], 'resumed')
        self.assertEqual(self.writes().count(['pane', 'run', PANE, self.writes()[0][3]]), 1)

    def test_open_stop_and_interrupt_writes_close_before_the_resume(self):
        self.up()
        self.sleep.side_effect = lambda seconds: self.fake.screens.update({PANE: composing('stop now and more')})
        self.at(0)
        self.assertEqual([w['kind'] for w in ops_panes.open_writes(self.conn, self.row['id'])], ['stop'])
        self.sleep.side_effect = None
        self.fake.settles = True
        self.comes_back()
        self.at(900)
        self.assertEqual(self.agent()['outcome'], 'resumed')
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id']), [])

    def test_a_hub_down_for_the_stop_still_brings_its_rows_back(self):
        self.up()
        self.fake.settles = True
        self.comes_back()
        self.at(900)
        self.assertEqual(self.agent()['outcome'], 'resumed')
        self.assertEqual(self.agent()['stopped'], 'left-for-kill')


class Failed(Overnight):
    def failed_resume(self):
        self.up()
        self.at(0)
        self.fake.calls.clear()
        self.comes_back(boots=False)
        self.at(900)

    def test_codex_resume_failure_flags_seat_and_placement_and_starts_fresh(self):
        self.failed_resume()
        agent = self.agent()
        self.assertEqual((agent['outcome'], agent['seat'], agent['placement']), ('resume-failed', 'seat-x', 'wsl'))
        self.assertTrue(agent['reason'])
        self.assertTrue(self.answer['settled'])
        self.assertEqual(self.outcome(), 'compaction-fresh')
        self.assertEqual(self.new_row()['state'], 'failed')
        self.assertTrue(self.role()['pending_wake'])
        self.assertLessEqual(len(self.pane_notices()), 1, 'the launch may say so once; the morning report is the flag')
        compaction.report(self.state, dict(status='compacted', errors=[], panes=self.answer), NOW)
        notice, = compaction.disk_state.read(self.state)['outbox'].values()
        self.assertIn('seat-x (wsl)', notice['notice']['body'])

    def test_fresh_agent_starts_only_after_the_hold_is_released(self):
        self.failed_resume()
        with mock.patch.object(pane_launch, 'start') as start:
            self.tick()
            start.assert_not_called()
            self.released()
            self.tick()
        start.assert_called_once()
        self.assertEqual(start.call_args.args[2]['mode'], 'fresh')
        self.assertEqual(start.call_args.args[2]['seat'], 'seat-x')

    def test_a_cleared_wake_does_not_lose_the_fresh_agent(self):
        self.failed_resume()
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        self.released()
        with mock.patch.object(pane_launch, 'start') as start:
            self.tick()
        start.assert_called_once()

    def test_each_cause_ends_the_row_and_never_leaves_it_in_compaction(self):
        for name, value in (('side_effect', AdmissionHeld('pane-open-write')), ('return_value', dict(state='failed'))):
            with self.subTest(name):
                self.setUp()
                self.up()
                self.at(0)
                self.comes_back(boots=False)
                with mock.patch.object(pane_launch, 'resume', **{name: value}):
                    self.at(900)
                self.assertEqual(self.agent()['outcome'], 'resume-failed')
                self.assertEqual(self.outcome(), 'compaction-fresh')
                self.assertEqual(self.new_row()['state'], 'failed')

    def test_the_role_is_signalled_after_the_run_has_finished(self):
        self.up()
        self.at(0)
        self.comes_back(boots=False)
        seen = []
        real = pane_compaction.roles.signal
        with mock.patch.object(pane_compaction.roles, 'signal',
                               lambda conn, role, event: (seen.append((role, self.outcome())), real(conn, role, event))):
            self.at(900)
        self.assertEqual(seen, [(ROLE, 'compaction-fresh')])

    def test_a_run_that_ended_during_the_hold_is_not_resumed(self):
        self.up()
        self.at(0)
        self.runtime.finish(self.rid, dict(status='stopped', usage={}))
        self.fake.calls.clear()
        self.comes_back(boots=False)
        self.at(900)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resume-failed', 'no-open-run'))
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.outcome(), 'stopped')

    def test_a_resume_cut_short_by_a_crash_is_never_launched_again(self):
        self.up()
        self.at(0)
        self.comes_back(boots=False)
        with mock.patch.object(pane_launch, 'resume', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.at(900)
        self.hub_restart()
        with mock.patch.object(pane_launch, 'resume') as again:
            self.at(920)
        again.assert_not_called()
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resume-failed', 'resume-cut-short'))
        self.assertEqual(self.new_row()['state'], 'failed')

    def test_a_tick_past_the_resume_deadline_starts_no_launch(self):
        self.up()
        self.at(0)
        self.comes_back(boots=False)
        with mock.patch.object(pane_launch, 'resume') as late:
            self.at(900 + compaction_hold.RESUME_S)
        late.assert_not_called()
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resume-failed', 'no-time-left'))

    def test_the_job_has_an_answer_before_a_slow_launch(self):
        self.up()
        self.at(0)
        self.comes_back(boots=False)
        seen = []
        with mock.patch.object(pane_launch, 'resume', lambda *a, **k: seen.append(self.saved()) or dict(state='failed')):
            self.at(900)
        self.assertEqual((seen[0]['phase'], seen[0]['settled']), ('resume', False))

    def test_an_exception_in_one_resume_stops_nothing(self):
        self.up()
        self.at(0)
        self.comes_back(boots=False)
        with mock.patch.object(pane_launch, 'resume', side_effect=KeyError('path')), self.assertLogs(pane_compaction.log, 'WARNING'):
            self.at(900)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resume-failed', 'resume-error'))

    def test_an_agent_that_never_takes_the_text_fails_at_the_deadline(self):
        self.up()
        self.at(0)
        self.comes_back()
        self.fake.after[('pane', 'run', 2)] = lambda: (self.boot(), self.fake.agents[PANE].update(focused=True))
        self.at(900)
        self.assertIsNone(self.agent()['outcome'])
        self.at(900 + compaction_hold.RESUME_S)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resume-failed', 'focused'))
        self.assertEqual(self.new_row()['state'], 'failed')
        self.released()
        with mock.patch.object(pane_launch, 'start') as start:
            self.tick()
        start.assert_not_called()       # its agent is still in the pane, and two agents on a role is worse than a wait

    def test_herdr_closed_is_one_flag_and_the_job_finishes(self):
        self.up()
        self.fake.closed = True
        self.at(0)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('left-for-kill', 'herdr-unavailable'))
        self.assertEqual(len(self.pane_notices()), 1)
        self.at(360)
        self.assertTrue(self.answer['settled'])
        self.restarted()
        self.at(900)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resume-failed', 'herdr-unavailable'))
        self.assertTrue(self.answer['settled'])
        self.assertEqual(self.outcome(), 'compaction-fresh')
        self.assertEqual(len(self.pane_notices()), 1)

    def test_herdr_closing_at_the_resume_is_noticed_there(self):
        self.up()
        self.at(0)
        self.comes_back(boots=False)
        self.fake.closed = True
        self.at(900)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resume-failed', 'herdr-unavailable'))
        self.assertEqual(len(self.pane_notices()), 1)

    def test_a_row_a_hold_left_behind_is_ended_and_flagged(self):
        self.up()
        self.at(0)
        self.killed()
        self.released()
        self.panes.compaction_tick(None)
        self.assertEqual(self.outcome(), 'compaction-fresh')
        self.assertEqual(self.new_row()['state'], 'failed')
        notice, = self.pane_notices()
        self.assertIn('seat-x (wsl)', notice['data']['notice']['body'])
        self.assertTrue(self.role()['pending_wake'])

    def test_no_hold_and_no_leftovers_is_nothing(self):
        self.launched()
        calls = len(self.fake.calls)
        self.panes.compaction_tick(None)
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(len(self.fake.calls), calls)

    def test_the_dispatcher_gives_the_panes_each_hold_and_the_moment_it_ends(self):
        hub = make_dispatcher(self.port, launcher=InProcessLauncher(str(self.state / 'runs')), now=lambda: self.time)
        hub.panes = mock.Mock()
        with mock.patch('drover.compaction.recover', return_value=False), \
                mock.patch.object(Dispatcher, '_compaction_drain', return_value={}), mock.patch.object(Dispatcher, 'check_stale'):
            self.hold_ = dict(id='h1', phase='draining')
            hub.tick()
            hub.panes.compaction_tick.assert_called_with(self.hold_)
            self.hold_ = None
            hub.tick()
            hub.panes.compaction_tick.assert_called_with(None)

    def test_a_failing_compaction_tick_never_leaves_the_dispatcher(self):
        hub = SimpleNamespace(panes=mock.Mock())
        hub.panes.compaction_tick.side_effect = KeyError('C:/secret')
        with self.assertLogs('drover.dispatcher', 'WARNING') as logs:
            Dispatcher._pane_compaction(hub, dict(id='h1'))
        self.assertNotIn('secret', logs.output[0])


if __name__ == '__main__':
    unittest.main()
