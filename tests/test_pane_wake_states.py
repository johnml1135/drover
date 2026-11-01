"""What becomes of a wake past the send: the hour's budget, the open `wake_unverified` write, and the queue."""
import unittest
from unittest import mock

from drover import pane_send, pane_wake
from drover.ledger import ops_panes
from drover.pane_wake import TEXT, WEDGED_S
from tests.pane_send_support import CLAUDE_IDLE, DRAFT, IDLE, WORKING
from tests.pane_wake_support import CLAUDE_WORKING, LATE, QUEUED, QUEUED_IDLE, WINDOW_S, Stage
from tests.panes_support import ROLE


class Budget(Stage):
    def spend(self, count=6):
        for index in range(count):
            self.notifications.reserve_wake(ROLE, f'spent:{index}')

    def test_a_spent_hour_holds_one_question_and_the_wake_waits_for_the_hour(self):
        self.scene()
        self.spend()
        self.mail()
        for _ in range(3):
            self.time += WINDOW_S
            self.deliver()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.role_questions(), 1)
        self.assertEqual(self.budget(), 6)
        self.assertTrue(self.pending())
        self.time += 3600
        self.deliver()
        self.assertEqual(len(self.sends()), 1)      # a held answer is not remembered for the mail's position
        self.assertEqual(self.role_questions(), 0)

    def test_a_held_budget_answer_needs_a_safe_pane_first(self):
        self.scene(DRAFT)
        self.spend()
        self.mail()
        self.time += WINDOW_S
        self.deliver()
        self.assertEqual(self.role_questions(), 0)

    def test_a_live_lease_coalesces_and_the_next_tick_can_still_wake(self):
        self.scene()
        self.mail()
        self.notifications.acquire(ROLE, 's1')
        self.mail()
        self.deliver()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.budget(), 0)
        self.time += 61                             # the default lease has gone
        self.deliver()
        self.assertEqual(len(self.sends()), 1)

    def test_a_second_wake_at_the_same_position_is_charged_and_waits_its_window(self):
        self.woken()
        self.show(CLAUDE_WORKING, 'working')
        self.deliver()
        self.commit(dict(continuation=True))        # pending again, with no new event to move the position
        self.show(CLAUDE_IDLE)
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'acknowledged')
        self.assertTrue(self.pending())
        self.deliver()
        self.assertEqual(self.sends(), [])
        self.time += WINDOW_S
        self.deliver()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.budget(), 2)


class Unverified(Stage):
    def overdue(self, **kw):
        self.woken(**kw)
        self.time += LATE
        self.deliver()

    def test_a_turn_with_no_checkpoint_goes_unverified_too(self):
        self.woken()
        self.show(CLAUDE_WORKING, 'working')
        self.deliver()
        self.time += LATE
        self.deliver()
        self.assertEqual(self.wake_state(), ('wake_unverified', 'turn-seen'))
        self.assertEqual(len(self.notices()), 1)

    def test_wake_unverified_stays_open_and_refuses_every_later_send(self):
        self.overdue()
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id'])[0]['state'], 'wake_unverified')
        self.fake.calls.clear()
        self.refused(self.send, 'last-write-wake_unverified')
        self.refused(lambda: pane_send.send(self.port, self.herdr, self.row, 'hand off', 'handoff'), 'last-write-wake_unverified')

    def test_mail_flows_while_it_is_open(self):
        self.overdue()
        self.mail()
        self.deliver()
        self.assertEqual(self.sends(), [])
        self.assertTrue(self.notifications.receive(ROLE))
        self.assertTrue(self.pending())

    def test_it_closes_when_the_agent_acts_on_the_ledger(self):
        self.overdue()
        self.commit()
        self.deliver()
        self.assertEqual(self.wake_state(), ('observed', pane_wake.ACTED))
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id']), [])
        self.assertEqual(self.open_notices(), 0)
        self.mail()
        self.time += WINDOW_S
        self.deliver()
        self.assertEqual(len(self.sends()), 1)

    def test_a_turn_seen_after_the_deadline_still_counts_with_the_checkpoint(self):
        self.overdue()
        self.show(CLAUDE_WORKING, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('wake_unverified', 'turn-seen'))
        self.commit()
        self.deliver()
        self.assertEqual(self.wake_state(), ('acknowledged', 'turn-seen'))
        self.assertEqual(self.open_notices(), 0)

    def test_a_late_unverified_write_keeps_its_reason_and_can_never_get_an_enter(self):
        self.scene(IDLE, codex=True)
        self.mail()
        with mock.patch.object(pane_send, '_enter', return_value=None):
            self.deliver()
        self.fake.calls.clear()
        self.time += LATE
        self.deliver()
        self.assertEqual(self.wake_state(), ('wake_unverified', 'pasted'))
        self.refused(lambda: pane_send.submit_own(self.port, self.herdr, self.row), 'nothing-unsent')
        self.show(WORKING, 'working')       # never proved to land, so this turn may be another's
        self.deliver()
        self.assertEqual(self.wake_state(), ('wake_unverified', 'pasted'))

    def test_a_write_with_no_recorded_revision_never_reads_as_acted(self):
        self.scene()
        ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)['id'], 'sent')
        self.commit()
        self.time += LATE
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'wake_unverified')

    def test_it_closes_when_the_row_rotates(self):
        self.overdue()
        ops_panes.update_agent(self.conn, self.row['id'], state='retired')
        fresh = ops_panes.record_agent(self.conn, pane_id='w1:p2', terminal_id='t2', session_id='s2', kind='claude', seat='seat',
                                       placement='windows', started_by='drover', state='live', role_id=ROLE, run_id=2)
        pane_wake.deliver(self.service, dict(run_id=2, role_id=ROLE))
        self.assertEqual(self.wake_state(), ('observed', pane_wake.ROTATED))
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id']), [])
        self.assertEqual(self.open_notices(), 0)
        self.assertIsNone(ops_panes.last_write(self.conn, fresh['id'], 'wake'))

    def test_a_rotation_leaves_other_roles_and_other_writes_alone(self):
        self.overdue()
        other = ops_panes.record_agent(self.conn, pane_id='w1:p3', terminal_id='t3', kind='claude', seat='seat', placement='windows',
                                       started_by='drover', state='retired', role_id='director:b', run_id=3)
        theirs = ops_panes.note_write(self.conn, other['id'], 'wake', TEXT)
        brief = ops_panes.note_write(self.conn, self.row['id'], 'brief', 'a brief')
        running = ops_panes.record_agent(self.conn, pane_id='w1:p4', terminal_id='t4', kind='claude', seat='seat', placement='windows',
                                         started_by='drover', state='compaction', role_id=ROLE, run_id=4)
        running_wake = ops_panes.note_write(self.conn, running['id'], 'wake', TEXT)
        ops_panes.update_agent(self.conn, self.row['id'], state='retired')
        ops_panes.record_agent(self.conn, pane_id='w1:p2', terminal_id='t2', kind='claude', seat='seat', placement='windows',
                               started_by='drover', state='live', role_id=ROLE, run_id=2)
        pane_wake.deliver(self.service, dict(run_id=2, role_id=ROLE))
        self.assertEqual(ops_panes.last_write(self.conn, other['id'])['state'], theirs['state'])
        self.assertEqual({w['id'] for w in ops_panes.open_writes(self.conn, self.row['id'])}, {brief['id']})
        self.assertEqual(ops_panes.last_write(self.conn, running['id'])['state'], running_wake['state'])


class Queued(Stage):
    def test_a_queued_wake_is_queued_not_observed(self):
        self.woken(IDLE, codex=True)
        self.show(QUEUED, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('queued', None))
        self.assertEqual([w['id'] for w in ops_panes.open_writes(self.conn, self.row['id'])], [self.last()['id']])

    def test_a_drained_queue_shows_its_turn(self):
        self.woken(IDLE, codex=True)
        self.show(QUEUED, 'working')
        self.deliver()
        self.show(WORKING, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('observed', 'turn-seen'))

    def test_another_prompt_in_the_queue_does_not_hold_it(self):
        self.woken(IDLE, codex=True)
        self.show(QUEUED.replace(TEXT, TEXT[:30]))
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'observed')      # a running turn, and no wake in the queue
        self.show(f'x {TEXT}')
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'observed')

    def test_the_15_minutes_apply_to_a_queued_write(self):
        self.woken(IDLE, codex=True)
        self.show(QUEUED, 'working')
        self.deliver()
        self.time += LATE
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'wake_unverified')
        self.assertEqual(len(self.notices()), 1)

    def test_the_queue_proves_an_unverified_write_landed(self):
        self.scene(IDLE, codex=True)
        self.mail()
        with mock.patch.object(pane_send, '_enter', return_value=None):
            self.deliver()
        self.assertEqual(self.wake_state(), ('unverified', 'pasted'))
        self.show(QUEUED, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('queued', None))
        self.show(WORKING, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('observed', 'turn-seen'))

    def test_the_queue_moves_no_write_backwards(self):
        self.woken(IDLE, codex=True)
        self.show(WORKING, 'working')
        self.deliver()
        self.show(QUEUED, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('observed', 'turn-seen'))
        self.time += LATE
        self.deliver()
        self.deliver()
        self.assertEqual(self.wake_state(), ('wake_unverified', 'turn-seen'))

    def test_a_settled_wake_takes_its_wedged_notice_with_it(self):
        self.woken(IDLE, codex=True)
        self.show(QUEUED_IDLE)
        self.deliver()
        self.time += WEDGED_S
        self.deliver()
        self.assertEqual(self.open_notices(), 1)
        self.show(WORKING, 'working')
        self.deliver()
        self.commit()
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'acknowledged')
        self.assertEqual(self.open_notices(), 0)

    def test_a_turn_under_the_queue_restarts_the_wedge_clock(self):
        self.woken(IDLE, codex=True)
        self.show(QUEUED_IDLE)
        self.deliver()
        self.time += WEDGED_S - 1
        self.show(QUEUED, 'working')
        self.deliver()
        self.time += 1
        self.show(QUEUED_IDLE)
        self.deliver()
        self.assertEqual(self.notices(), [])


if __name__ == '__main__':
    unittest.main()
