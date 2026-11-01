"""A due wake: to a safe pane once, or left in the mailbox. Rules 5, 8, 15 and 16 of the plan's table, and their rulings."""
import re
import unittest
from unittest import mock

from drover import compaction_hold, pane_send, pane_wake
from drover.ledger import ops_panes
from drover.pane_wake import TEXT, UNVERIFIED_S, WEDGED_S
from drover.panes import PaneService
from tests.pane_send_support import CLAUDE_IDLE, DIALOG, DRAFT, IDLE, PANE
from tests.pane_wake_support import CLAUDE_PAST_PROMPT, CLAUDE_WORKING, LATE, QUEUED_IDLE, WINDOW_S, Stage
from tests.panes_support import ROLE, Resident


class Delivery(Stage):
    def test_a_due_wake_reaches_an_idle_empty_pane_once(self):
        self.scene()
        self.mail()
        self.deliver()
        self.assertEqual(self.sends(), [['agent', 'prompt', PANE, TEXT]])
        self.assertEqual(self.wake_state(), ('sent', None))
        self.assertEqual(self.budget(), 1)

    def test_a_wsl_agent_gets_the_same_line_by_paste(self):
        self.scene(IDLE, codex=True)
        self.mail()
        self.deliver()
        self.assertEqual(self.sends(), [['pane', 'send-text', PANE, f'\x1b[200~{TEXT}\x1b[201~'], ['pane', 'send-keys', PANE, 'enter']])
        self.assertEqual(self.wake_state(), ('sent', None))

    def test_nothing_goes_without_pending_mail(self):
        self.scene()
        self.deliver()
        self.assertEqual(self.fake.calls, [])

    def test_nothing_goes_before_the_window_since_the_last_wake(self):
        self.scene()
        self.notifications.reserve_wake(ROLE, 'an-earlier-wake')
        self.mail()
        self.time += 59                 # this mail is critical, so its window is the short one
        self.deliver()
        self.assertEqual(self.sends(), [])
        self.time += 1
        self.deliver()
        self.assertEqual(len(self.sends()), 1)

    def test_no_wake_during_a_compaction_hold(self):
        self.scene()
        self.mail()
        with mock.patch.object(compaction_hold, 'active', return_value=dict(id='h1')):
            self.deliver()
        self.stays_in_the_mailbox()
        self.assertEqual(self.fake.calls, [])

    def test_a_row_that_is_not_live_gets_nothing(self):
        self.scene()
        self.mail()
        ops_panes.update_agent(self.conn, self.row['id'], state='compaction')
        self.deliver()
        self.stays_in_the_mailbox()
        self.assertIsNone(pane_wake.deliver(self.service, dict(run_id=404, role_id=ROLE)))

    def test_focused_pane_keeps_the_wake_pending(self):
        for kw in (dict(agent=dict(focused=True)), dict(pane=dict(focused=True))):
            with self.subTest(kw):
                self.scene(**kw)
                self.mail()
                self.deliver()
                self.stays_in_the_mailbox()
                self.reset()

    def test_a_pane_that_reads_none_or_refuses_spends_nothing(self):
        for screen, kw in ((CLAUDE_PAST_PROMPT, {}), (CLAUDE_WORKING, {}), (DIALOG, {}), (CLAUDE_IDLE, dict(agent=dict(agent_status='working')))):
            with self.subTest(screen):
                self.scene(screen, **kw)
                self.mail()
                self.deliver()
                self.stays_in_the_mailbox()
                self.reset()

    def test_a_refused_send_spends_no_budget(self):
        self.scene()
        self.mail()
        self.fake.after[('pane', 'read', 1)] = lambda: self.fake.screens.update({PANE: DRAFT})
        self.deliver()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.budget(), 0)
        self.assertTrue(self.pending())
        self.fake.screens[PANE] = CLAUDE_IDLE       # no window started, so the next tick may go
        self.deliver()
        self.assertEqual(self.budget(), 1)
        self.assertEqual(len(self.sends()), 1)

    def test_a_write_that_never_left_spends_no_budget_and_is_not_outstanding(self):
        self.scene()
        self.mail()
        self.fake.refuse[('agent', 'prompt')] = 'agent_blocked'     # refused by Herdr, after the note and the reserve
        self.deliver()
        self.assertEqual(self.wake_state(), ('unsent', 'herdr-refused:agent_blocked'))
        self.assertEqual(self.budget(), 0)
        del self.fake.refuse[('agent', 'prompt')]
        self.fake.calls.clear()
        self.deliver()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.wake_state(), ('sent', None))

    def test_owner_draft_is_never_noticed(self):
        self.scene(DRAFT)
        self.mail()
        for _ in range(4):
            self.deliver()
            self.time += LATE
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.budget(), 0)

    def test_another_open_write_reserves_nothing(self):
        self.scene()
        self.mail()
        ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, self.row['id'], 'handoff', 'hand off')['id'], 'unverified', 'pasted')
        with mock.patch.object(self.service.notifications, 'reserve_wake') as reserve:
            self.deliver()
        reserve.assert_not_called()
        self.assertEqual(self.sends(), [])


class Verification(Stage):
    def test_wake_counts_only_with_turn_activity_and_a_new_checkpoint(self):
        self.woken()
        self.deliver()
        self.assertEqual(self.wake_state(), ('sent', None))
        self.show(CLAUDE_WORKING, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('observed', 'turn-seen'))
        self.deliver()
        self.assertEqual(self.wake_state(), ('observed', 'turn-seen'))      # a turn alone is not delivery
        self.commit()
        self.deliver()
        self.assertEqual(self.wake_state(), ('acknowledged', 'turn-seen'))
        self.assertEqual(self.notices(), [])

    def test_a_checkpoint_with_no_turn_seen_is_not_a_delivered_wake(self):
        self.woken()
        self.commit()
        self.deliver()
        self.assertEqual(self.wake_state(), ('sent', None))
        self.show(CLAUDE_WORKING, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('acknowledged', 'turn-seen'))

    def test_a_checkpoint_never_matched_by_a_turn_ends_the_wake_with_no_notice(self):
        self.woken()
        self.commit()
        self.time += LATE
        self.deliver()
        self.assertEqual(self.wake_state(), ('observed', pane_wake.ACTED))
        self.assertEqual(self.notices(), [])
        self.mail()
        self.time += WINDOW_S
        self.deliver()
        self.assertEqual(len(self.sends()), 1)      # that ended it: the row is not stalled

    def test_either_status_or_a_dialog_is_turn_activity(self):
        for screen, status in ((CLAUDE_WORKING, 'idle'), (CLAUDE_IDLE, 'working'), (CLAUDE_IDLE, 'blocked'), (DIALOG, 'idle')):
            with self.subTest(status=status, screen=screen):
                self.woken()
                self.show(screen, status)
                self.deliver()
                self.assertEqual(self.wake_state(), ('observed', 'turn-seen'))
                self.reset()
                self.conn.execute('DELETE FROM director_budget_entries')

    def test_a_refused_read_changes_nothing_but_the_clock_still_runs(self):
        self.woken()
        self.fake.agents[PANE]['agent_session'] = {'value': 'someone-else'}
        self.deliver()
        self.assertEqual(self.wake_state(), ('sent', None))
        self.time += LATE
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'wake_unverified')

    def test_one_wake_outstanding_at_a_time(self):
        self.woken()
        for screen, status in ((CLAUDE_IDLE, 'idle'), (CLAUDE_WORKING, 'working'), (CLAUDE_IDLE, 'idle')):
            self.show(screen, status)
            self.mail()
            self.time += 200
            self.deliver()
            self.assertEqual(self.sends(), [])
            self.assertEqual(self.budget(), 1)
        self.commit()
        self.mail()
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'acknowledged')
        self.assertEqual(self.sends(), [])
        self.deliver()
        self.assertEqual(len(self.sends()), 1)      # only a settled wake lets the next one go

    def test_a_write_left_unverified_is_not_read_as_landed_from_a_status_alone(self):
        self.scene()
        self.mail()
        self.fake.hang = {('agent', 'prompt')}
        self.deliver()
        self.fake.hang = False
        self.assertEqual(self.wake_state(), ('unverified', 'herdr-unavailable'))
        self.assertEqual(self.budget(), 1)      # it may have landed, so it is spent
        self.show(CLAUDE_WORKING, 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('unverified', 'herdr-unavailable'))

    def test_an_unverified_write_closes_when_its_own_prompt_and_turn_are_on_screen(self):
        self.scene(IDLE, codex=True)
        self.mail()
        with mock.patch.object(pane_send, '_enter', return_value=None):
            self.deliver()
        self.assertEqual(self.wake_state()[0], 'unverified')
        self.show(f'› {TEXT}\n\n• Working (3s • esc to interrupt)\n\n› \n\n? for shortcuts\n', 'working')
        self.deliver()
        self.assertEqual(self.wake_state(), ('observed', 'turn-seen'))

    def test_unverified_wake_is_not_replayed_after_hub_restart(self):
        self.scene()
        self.mail()
        note = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)    # the hub died between the note and the call
        self.time += WINDOW_S
        self.service = PaneService(self.port, self.herdr, cfg={}, now=lambda: self.time)
        self.deliver()
        self.assertEqual(self.sends(), [])
        self.assertEqual(ops_panes.last_write(self.conn, self.row['id'])['id'], note['id'])

    def test_sent_wake_is_followed_after_hub_restart(self):
        self.woken()
        self.service = PaneService(self.port, self.herdr, cfg={}, now=lambda: self.time)
        self.show(CLAUDE_WORKING, 'working')
        self.deliver()
        self.commit()
        self.service = PaneService(self.port, self.herdr, cfg={}, now=lambda: self.time)
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'acknowledged')


class Overdue(Stage):
    def test_wake_not_acknowledged_in_15_minutes_is_one_notice_and_no_resend(self):
        self.woken()
        self.time += UNVERIFIED_S - 2
        self.deliver()
        self.assertEqual(self.wake_state(), ('sent', None))
        self.time += 4
        self.mail()
        for _ in range(3):
            self.deliver()
            self.time += WINDOW_S
        self.assertEqual(self.wake_state()[0], 'wake_unverified')
        self.assertEqual(self.sends(), [])
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.budget(), 1)

    def test_queue_idle_ten_minutes_is_wedged_and_sends_no_key(self):
        self.woken(IDLE, codex=True)
        self.show(QUEUED_IDLE)
        self.deliver()
        self.assertEqual(self.wake_state()[0], 'queued')
        self.time += WEDGED_S - 1
        self.deliver()
        self.assertEqual(self.notices(), [])
        self.time += 1
        self.deliver()
        self.deliver()
        self.assertEqual([n['id'] for n in self.notices()], [f"pane-wake-wedged:{self.last()['id']}"])
        self.assertEqual(self.sends(), [])


class Text(Stage):
    def test_wake_text_has_no_digit_and_grants_nothing(self):
        self.assertIsNone(re.search(r'\d', TEXT))
        self.assertEqual(pane_send.TYPED_KINDS.count('wake'), 1)
        self.assertLessEqual(len(TEXT), pane_send.MAX_TEXT)
        for word in ('approve', 'allow', 'yes', 'model', 'restart', 'exit', '/compact', 'trust', '/'):
            self.assertNotIn(word, TEXT.lower())

    def test_wake_carries_no_message_body(self):
        self.scene()
        self.notifications.broadcast('owner', [ROLE], 'a very private body', urgency='urgent')
        self.notifications.project()
        self.deliver()
        self.assertEqual(self.sends(), [['agent', 'prompt', PANE, TEXT]])
        self.assertNotIn('private', ' '.join(str(c) for c in self.fake.calls))

    def test_mail_stays_pending_until_the_director_commits(self):
        self.woken()
        self.show(CLAUDE_WORKING, 'working')
        self.deliver()
        self.assertTrue(self.pending())
        self.assertTrue(self.notifications.receive(ROLE))
        self.commit()
        self.assertFalse(self.pending())
        self.assertFalse(self.notifications.receive(ROLE))


class ThroughTheTick(Resident):
    def test_a_live_director_is_woken_by_the_runtime_tick(self):
        self.launched()
        self.fake.screens[PANE] = CLAUDE_IDLE
        self.fake.agents[PANE]['agent_status'] = 'idle'
        self.time += WINDOW_S
        before = len(self.calls('agent', 'prompt'))
        self.tick()
        self.assertEqual(self.calls('agent', 'prompt')[before:], [['agent', 'prompt', PANE, TEXT]])
        self.assertEqual(ops_panes.last_write(self.conn, self.row['id'], 'wake')['state'], 'sent')


if __name__ == '__main__':
    unittest.main()
