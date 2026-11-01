"""What a resident does when it cannot go on: Herdr closed before a launch, holds on an alive tick, lasting unknown, and a lapsed session."""
import unittest
from unittest import mock

from drover import compaction_hold, pane_launch, panes
from drover.launch import AdmissionHeld
from drover.ledger import ops_panes
from drover.ledger.db import tx
from drover.notifications import roles
from tests.pane_launch_support import LONE_SHELL, PROMPT, SECRET
from tests.pane_send_support import CLAUDE_IDLE, PANE
from tests.panes_support import ROLE, Resident

CTRL_C = ['pane', 'send-keys', PANE, 'ctrl+c']
DAY_AND_A_BIT = 90000


class Case(Resident):
    def hold(self, reason):
        with tx(self.conn):
            roles._hold_role(self.notifications, ROLE, reason)

    def spent(self):
        count = lambda table: self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        return count('director_budget_entries'), count('director_runs'), self.role()['generation']

    def ticks(self, count, step=30):
        for _ in range(count):
            self.time += step
            self.tick()

    def notes(self, prefix):
        return [n for n in self.notices() if n['id'].startswith(prefix)]



class ClosedBeforeALaunch(Case):
    def test_a_closed_herdr_spends_nothing_and_the_launch_follows_its_return(self):
        self.build()
        self.turns()
        self.fake.closed = True
        self.wake()
        before = self.spent()
        self.ticks(10)
        self.assertEqual(self.spent(), before)
        self.assertEqual(self.open_questions(), 0)
        self.assertIsNone(self.role()['held_reason'])
        self.assertEqual(self.calls('tab', 'create'), [])
        self.assertEqual(len(self.notes('pane-herdr-closed:')), 1)
        self.assertTrue(self.role()['pending_wake'], 'the mail waits')
        self.fake.closed = False
        self.time += 30
        self.assertEqual(len(self.tick()['launched']), 1)

    def test_a_failed_run_spends_no_replacement_while_herdr_is_closed(self):
        self.launched()
        ops_panes.update_agent(self.conn, self.row['id'], state='failed')
        self.tick()
        self.fake.closed = True
        generation = self.role()['generation']
        self.wake()
        self.ticks(3)
        self.assertEqual(self.role()['replacements_used'], 0)
        self.assertEqual(self.role()['generation'], generation)

    def test_a_role_held_by_the_outage_frees_when_herdr_returns(self):
        self.build()
        self.hold('herdr-unavailable')
        self.fake.closed = True
        self.ticks(2)
        self.assertEqual(self.role()['held_reason'], 'herdr-unavailable')
        self.fake.closed = False
        self.ticks(1)
        self.assertIsNone(self.role()['held_reason'])
        self.assertEqual(self.open_questions(), 0)

    def test_seeing_herdr_back_closes_the_outage_with_no_launch_to_do_it(self):
        self.build()
        self.fake.closed = True
        self.wake()
        self.ticks(1)
        self.capacity['slots'] = 0
        self.fake.closed = False
        self.ticks(1)
        event = next(e for e in self.port.events(kind='pane.notice') if e['data']['notice']['id'].startswith('pane-herdr-closed:'))
        self.assertEqual(self.conn.execute('SELECT status FROM questions WHERE id=?', (event['data']['question'],)).fetchone()[0],
                         'answered')

    def test_another_hold_is_not_answered_by_herdr_returning(self):
        self.build()
        self.hold('wake-budget-exhausted')
        self.ticks(1)
        self.assertEqual(self.role()['held_reason'], 'wake-budget-exhausted')

    def test_no_launch_is_tried_during_a_compaction_hold(self):
        self.build()
        self.wake()
        before = self.spent()
        with mock.patch.object(compaction_hold, 'active', return_value=dict(id='h1')):
            self.ticks(2)
        self.assertEqual(self.spent(), before)
        self.assertEqual(self.calls('tab', 'create'), [])

    def test_a_tick_with_no_mail_asks_nothing_of_herdr(self):
        self.build()
        self.tick()
        self.assertEqual(self.calls('workspace', 'list'), [])
        self.assertEqual(self.notes('pane-herdr-closed:'), [])


class AliveTicks(Case):
    def test_an_alive_tick_leaves_a_budget_hold_open(self):
        self.launched()
        self.hold('wake-budget-exhausted')
        self.ticks(2)
        self.assertEqual(self.role()['held_reason'], 'wake-budget-exhausted')
        self.assertEqual(self.open_questions(), 1)

    def test_an_alive_tick_answers_every_hold_liveness_raised(self):
        self.launched()
        for reason in ('consumer-liveness-uncertain', 'consumer-observation-uncertain', 'adoption-uncertain',
                       'launch-effect-unknown', 'herdr-unavailable'):
            with self.subTest(reason):
                self.hold(reason)
                self.ticks(1)
                self.assertIsNone(self.role()['held_reason'])
                self.assertEqual(self.open_questions(), 0)


class LastingUnknown(Case):
    def unreadable(self):
        del self.fake.panes[PANE]['terminal_id']

    def readable(self):
        self.fake.panes[PANE]['terminal_id'] = 't1'

    def test_past_thirty_minutes_is_one_notice_and_one_question(self):
        self.launched()
        self.unreadable()
        self.ticks(1)
        self.ticks(1, panes.UNKNOWN_LIMIT_S - 1)
        self.assertEqual(self.notes('pane-unknown:'), [])
        self.assertEqual(self.open_questions(), 0)
        self.ticks(1, 2)
        self.ticks(4)
        self.assertEqual(len(self.notes('pane-unknown:')), 1)
        self.assertEqual(self.open_questions(), 1)
        self.assertEqual(self.role()['held_reason'], 'consumer-liveness-uncertain')

    def test_it_renews_until_the_limit_and_never_after(self):
        self.launched()
        self.time += 44000
        first = self.expiry()
        self.unreadable()
        self.ticks(1, 0)
        self.assertGreater(self.expiry(), first, 'a short doubt renews')
        self.ticks(1, panes.UNKNOWN_LIMIT_S + 1)
        renewed = self.expiry()
        self.ticks(1, 50000)
        self.assertEqual(self.expiry(), renewed)

    def test_a_clear_read_ends_the_episode_and_the_next_is_its_own(self):
        self.launched()
        for _ in range(2):
            self.unreadable()
            self.ticks(1)
            self.ticks(3, panes.UNKNOWN_LIMIT_S)
            self.assertEqual(self.open_questions(), 1)
            self.readable()
            self.ticks(1)
            self.assertEqual(self.open_questions(), 0)
        self.assertEqual(len(self.notes('pane-unknown:')), 2)

    def test_a_clear_read_closes_the_notice(self):
        self.launched()
        self.unreadable()
        self.ticks(1)
        self.ticks(2, panes.UNKNOWN_LIMIT_S)
        self.readable()
        self.ticks(1)
        event = next(e for e in self.port.events(kind='pane.notice') if e['data']['notice']['id'].startswith('pane-unknown:'))
        self.assertEqual(self.conn.execute('SELECT status FROM questions WHERE id=?', (event['data']['question'],)).fetchone()[0],
                         'answered')

    def test_an_outage_is_not_counted(self):
        self.launched()
        self.fake.closed = True
        self.ticks(1)
        self.ticks(3, panes.UNKNOWN_LIMIT_S)
        self.assertEqual(self.notes('pane-unknown:'), [])
        self.assertEqual(self.open_questions(), 0)

    def test_an_agent_that_left_is_not_lasting_unknown(self):
        self.launched()
        del self.fake.agents[PANE]
        self.fake.processes = LONE_SHELL
        self.ticks(1)
        self.ticks(3, panes.UNKNOWN_LIMIT_S)
        self.assertEqual(self.notes('pane-unknown:'), [])
        self.assertEqual(len(self.notes('pane-agent-left:')), 1)


class Lapsed(Case):
    def exited(self):
        del self.fake.agents[PANE]
        self.fake.processes = LONE_SHELL
        self.fake.screens[PANE] = PROMPT
        seen = self.fake.seen.get(('agent', 'start'), 0)
        self.fake.after[('agent', 'start', seen + 1)] = self.boot

    def idle(self):
        self.fake.screens[PANE], self.fake.agents[PANE]['agent_status'] = CLAUDE_IDLE, 'idle'

    def test_a_lapsed_session_revives_on_its_own_pane_on_a_new_row_and_run(self):
        self.launched()
        generation = self.role()['generation']
        self.exited()
        self.time += DAY_AND_A_BIT
        self.wake()
        launched = self.tick()['launched']
        self.assertEqual(len(launched), 1)
        self.assertEqual(self.outcome(), 'revived')
        fresh = ops_panes.by_run(self.conn, launched[0])
        self.assertEqual((fresh['pane_id'], fresh['state'], fresh['session_id']), (PANE, 'live', 's1'))
        self.assertNotEqual(fresh['id'], self.row['id'])
        self.assertEqual(self.row_now()['state'], 'gone')
        self.assertEqual(len(self.calls('tab', 'create')), 1)
        self.assertEqual(self.calls('agent', 'start')[-1][8:10], ['--resume', 's1'])
        self.assertEqual(self.role()['replacements_used'], 0)
        self.assertEqual(self.role()['generation'], generation + 1)

    def test_it_needs_no_mail_to_come_back(self):
        self.launched()
        self.exited()
        self.conn.execute('UPDATE notification_inbox SET resolved=1')
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        self.conn.commit()
        self.time += DAY_AND_A_BIT
        self.assertEqual(len(self.tick()['launched']), 1)

    def test_the_revived_agent_holds_a_new_credential(self):
        self.launched()
        self.exited()
        self.time += DAY_AND_A_BIT
        rid, = self.tick()['launched']
        path, text, _ = self.private[-1]
        self.assertEqual(len(self.private), 2)
        self.assertIn(f'panes\\{rid}\\', path.replace('/', '\\'))
        self.assertEqual(self.notifications.authenticate(text)['generation'], self.role()['generation'])
        self.assertFalse(any(text in str(call) for call in self.fake.calls))
        self.assertFalse(any(text in str(event) for event in self.port.events()))

    def test_a_revived_row_gets_no_brief(self):
        self.launched()
        self.exited()
        self.time += DAY_AND_A_BIT
        rid, = self.tick()['launched']
        self.ticks(3)
        self.assertIsNone(ops_panes.last_write(self.conn, ops_panes.by_run(self.conn, rid)['id'], 'brief'))
        self.assertIsNone(self.outcome(rid))

    def test_no_interrupt_goes_to_a_lapsed_agent_that_is_still_there(self):
        self.launched()
        self.idle()
        self.time += DAY_AND_A_BIT
        self.wake()
        self.ticks(4)
        self.assertEqual(self.calls('pane', 'send-keys'), [])
        self.assertEqual(len(self.calls('tab', 'create')), 1, 'never a second agent beside it')
        self.assertEqual([r['run_id'] for r in self.runtime.active_runs()], [self.rid])
        self.assertEqual(len(self.notes('pane-lapsed:')), 1)
        self.assertIn('closing the pane', self.notes('pane-lapsed:')[0]['body'].lower())

    def test_it_comes_back_once_that_agent_exits(self):
        self.launched()
        self.idle()
        self.time += DAY_AND_A_BIT
        self.ticks(2)
        self.exited()
        self.ticks(1)
        self.assertEqual(self.outcome(), 'revived')
        self.assertEqual(len(self.runtime.active_runs()), 1)

    def test_no_wake_goes_to_a_lapsed_agent(self):
        self.launched()
        wake, _ = self.hooks()
        self.time += DAY_AND_A_BIT
        self.ticks(2)
        wake.assert_not_called()

    def test_an_unreadable_pane_at_expiry_changes_nothing(self):
        self.launched()
        self.exited()
        del self.fake.panes[PANE]['terminal_id']
        self.time += DAY_AND_A_BIT
        self.ticks(2)
        self.assertIsNone(self.outcome())
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_guard_that_refuses_at_the_resume_writes_nothing(self):
        self.launched()
        self.exited()

        def fenced():
            raise AdmissionHeld('stop-work')
        with self.assertRaises(AdmissionHeld):
            self.adapter._resume(self.spec_, SECRET, fenced)
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(len(self.calls('agent', 'start')), 1)

    def test_a_closed_herdr_at_expiry_changes_nothing_until_it_returns(self):
        self.launched()
        self.exited()
        self.fake.closed = True
        self.time += DAY_AND_A_BIT
        self.wake()
        self.ticks(3)
        self.assertEqual(self.calls('pane', 'send-keys'), [])
        self.assertIsNone(self.outcome())
        self.assertEqual(self.row_now()['state'], 'live')
        self.fake.closed = False
        self.ticks(1)
        self.assertEqual(self.outcome(), 'revived')

    def test_a_pause_keeps_the_lapsed_run_until_it_lifts(self):
        self.launched()
        self.exited()
        self.port.set_setting('dispatch_enabled', 'false')
        self.time += DAY_AND_A_BIT
        with self.assertRaises(AdmissionHeld) as caught:
            self.guard()
        self.assertEqual(str(caught.exception), 'director-session-expired')
        self.ticks(2)
        self.assertIsNone(self.outcome())
        self.assertEqual(self.open_questions(), 0)
        self.port.set_setting('dispatch_enabled', 'true')
        self.ticks(1)
        self.assertEqual(self.outcome(), 'revived')

    def test_a_refused_resume_flags_the_pane_and_goes_to_a_fresh_one(self):
        self.launched()
        self.exited()
        ops_panes.note_write(self.conn, self.row['id'], 'brief', 'a write that may yet land')
        self.time += DAY_AND_A_BIT
        self.tick()
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual(len(self.calls('tab', 'create')), 2)
        notice, = self.notes(f"pane-launch:{self.row['id']}")
        self.assertIn('seat-c', notice['body'])
        self.assertIn('resume refused: pane-open-write', notice['body'])
        self.assertIn('fresh agent', notice['body'])

    def test_a_refused_resume_never_opens_a_pane_beside_an_agent(self):
        self.launched()
        self.exited()
        self.time += DAY_AND_A_BIT
        real = pane_launch.resume

        def agent_came_back(*args, **kwargs):
            self.fake.processes = self.foreground
            return real(*args, **kwargs)
        with mock.patch.object(pane_launch, 'resume', agent_came_back):
            self.tick()
        self.assertEqual(len(self.calls('tab', 'create')), 1)
        self.assertEqual(self.role()['held_reason'], 'pane-role-unconfirmed')

    def refused_mid_resume(self, event):
        def refuses(*args, **kwargs):
            event()
            raise AdmissionHeld('pane-occupant-changed')
        return mock.patch.object(pane_launch, 'resume', refuses)

    def test_a_failed_read_inside_a_resume_changes_no_row(self):
        self.launched()
        self.exited()
        self.time += DAY_AND_A_BIT
        with self.refused_mid_resume(lambda: self.fake.panes[PANE].pop('terminal_id')):
            self.tick()
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(self.notes('pane-launch:'), [])
        self.assertEqual(len(self.calls('tab', 'create')), 1)

    def test_a_pane_closed_inside_a_resume_goes_to_a_fresh_one(self):
        self.launched()
        self.exited()
        self.time += DAY_AND_A_BIT
        with self.refused_mid_resume(lambda: self.fake.panes.pop(PANE)):
            self.tick()
        self.assertEqual(self.row_now()['state'], 'gone')
        self.assertEqual(len(self.calls('tab', 'create')), 2)

    def test_a_pane_that_cannot_be_read_before_the_resume_spends_nothing(self):
        self.launched()
        self.exited()
        self.capacity['slots'] = 0
        self.time += DAY_AND_A_BIT
        self.tick()
        self.assertEqual(self.outcome(), 'revived')
        before = self.spent()
        self.capacity['slots'] = 2
        del self.fake.panes[PANE]['terminal_id']
        self.ticks(3)
        self.assertEqual(self.spent(), before)
        self.assertEqual(self.role()['held_reason'], 'resume-verification-uncertain')
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(self.open_questions(), 1)

    def test_herdr_closing_inside_a_resume_changes_no_row(self):
        self.launched()
        self.exited()
        self.time += DAY_AND_A_BIT

        def closes(*args, **kwargs):
            self.fake.closed = True
            raise AdmissionHeld('pane-occupant-changed')
        with mock.patch.object(pane_launch, 'resume', closes):
            self.tick()
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(self.role()['held_reason'], 'herdr-unavailable')

    def test_a_resume_that_stalls_ends_on_a_fresh_pane(self):
        self.launched()
        self.exited()
        self.fake.after.clear()
        self.time += DAY_AND_A_BIT
        rid, = self.tick()['launched']
        self.assertEqual(ops_panes.open_agents(self.conn)[-1]['state'], 'failed')
        self.time += pane_launch.STARTUP_S + 1
        self.tick()
        self.assertEqual(self.outcome(rid), 'launch-failed')
        self.assertEqual(len(self.calls('tab', 'create')), 2)

    def test_stop_then_resume_revives_once_the_agent_is_out(self):
        self.launched()
        self.idle()
        self.notifications.stop('owner', 'stop')
        self.tick()
        self.assertEqual(self.calls('pane', 'send-keys'), [CTRL_C])
        self.assertIsNone(self.outcome(), 'STOP in force ends nothing')
        self.notifications.resume('owner', self.notifications.admission()['sequence'], 'go on')
        self.ticks(2)
        self.assertEqual(self.calls('pane', 'send-keys'), [CTRL_C], 'one stop, however the epoch moved')
        self.assertEqual(len(self.notes('pane-lapsed:')), 1)
        self.exited()
        self.ticks(1)
        self.assertEqual(self.outcome(), 'revived')
        self.assertEqual(len(self.runtime.active_runs()), 1)
        self.assertEqual(self.open_questions(), 0, 'its launch ends the stop question')

    def test_every_stop_tells_the_owner_how_the_role_comes_free(self):
        self.launched()
        self.idle()
        self.notifications.stop('owner', 'stop')
        self.ticks(3)
        notice, = self.notes('pane-stopped:')
        self.assertIn('closing the pane frees the role', notice['body'].lower())
        self.assertIn('seat-c', notice['body'])
        self.assertNotIn(SECRET, str(notice))

    def test_a_stop_that_pane_send_refuses_is_still_told(self):
        self.launched()
        self.idle()
        self.fake.panes[PANE]['focused'] = True
        self.fake.agents[PANE]['focused'] = True
        self.notifications.stop('owner', 'stop')
        self.ticks(2)
        self.assertEqual(self.calls('pane', 'send-keys'), [])
        self.assertEqual(len(self.notes('pane-stopped:')), 1)

    def test_a_skill_control_stops_and_tells(self):
        self.launched()
        self.idle()
        self.notifications.require_skill('owner', 'review', 'v3', [ROLE], 'affected')
        self.ticks(2)
        self.assertEqual(self.calls('pane', 'send-keys'), [CTRL_C])
        self.assertEqual(len(self.notes('pane-stopped:')), 1)
        self.assertIsNone(self.outcome())


if __name__ == '__main__':
    unittest.main()
