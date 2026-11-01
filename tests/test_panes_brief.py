"""After `live`, a launch is row state: the brief and the first turn go on later ticks, and no tick waits for either."""
import unittest
from unittest import mock

from drover import pane_launch, pane_send
from drover.launch import AdmissionHeld
from drover.ledger import ops_panes
from tests.pane_launch_support import LONE_SHELL
from tests.pane_send_support import CLAUDE_IDLE, PANE
from tests.panes_support import ROLE, Resident


class FakeSeconds:
    """`pane_launch` and `time.sleep` on one clock, so a test can read how long a tick really stood still."""

    def __init__(self, case):
        self.at = 0.0
        self.moved = lambda: None
        case.sleep.side_effect = self.wait
        patcher = mock.patch.object(pane_launch, '_now', lambda: self.at)
        patcher.start()
        case.addCleanup(patcher.stop)

    def wait(self, seconds):
        self.at += seconds
        self.moved()


class Case(Resident):
    def up(self, **scene):
        """A director that has just gone `live`: the launch is done and nothing has been briefed."""
        self.build(**scene)
        self.wake()
        self.rid, = self.tick()['launched']
        self.row = ops_panes.by_run(self.conn, self.rid)
        self.spec_ = self.runtime.active_runs()[0]['spec']
        self.assertEqual(self.row['state'], 'live')
        return self.row

    def briefs(self):
        return self.calls('agent', 'prompt')

    def later(self, seconds=30):
        self.time += seconds
        return self.tick()

    def working(self):
        self.fake.agents[PANE]['agent_status'] = 'working'

    def brief_write(self):
        return ops_panes.last_write(self.conn, self.row['id'], 'brief')


class TickSeconds(Case):
    def stand(self, **scene):
        clock = FakeSeconds(self)
        result = self.up(**scene)
        return clock, result

    def test_a_launch_stands_in_the_tick_no_longer_than_the_part_before_live(self):
        clock = FakeSeconds(self)
        self.build(boots=False)

        def slow_boot():
            clock.at += pane_launch.STARTUP_S - pane_launch.POLL_S
            self.boot()
        self.fake.after[('agent', 'start', 1)] = slow_boot
        self.wake()
        started = clock.at
        self.tick()
        limit = pane_launch.SHELL_PROMPT_S + pane_launch.SHELL_BACK_S + pane_launch.STARTUP_S
        self.assertLessEqual(clock.at - started, limit)

    def test_every_wait_at_its_limit_is_the_bound(self):
        clock = FakeSeconds(self)
        self.build(boots=False)
        self.fake.processes = self.foreground
        margin = pane_launch.POLL_S
        free_at = dict(at=pane_launch.SHELL_PROMPT_S - margin)

        def moved():
            if clock.at >= free_at['at']:
                self.fake.processes = LONE_SHELL

        def shell_line_taken():
            self.fake.processes = self.foreground
            free_at['at'] = clock.at + pane_launch.SHELL_BACK_S - margin

        def agent_up_and_silent():
            free_at['at'] = float('inf')
            self.fake.processes = self.foreground
        clock.moved = moved
        self.fake.after[('pane', 'run', 1)] = shell_line_taken
        self.fake.after[('agent', 'start', 1)] = agent_up_and_silent
        self.wake()
        self.tick()
        limit = pane_launch.SHELL_PROMPT_S + pane_launch.SHELL_BACK_S + pane_launch.STARTUP_S
        self.assertEqual(self.reason(), 'agent-not-ready')
        self.assertLessEqual(clock.at, limit)
        self.assertGreaterEqual(clock.at, limit - 3 * margin)

    def test_a_pane_that_never_shows_a_prompt_costs_the_same_bound(self):
        clock = FakeSeconds(self)
        self.build(boots=False)
        self.fake.processes = self.foreground
        self.wake()
        self.tick()
        self.assertLessEqual(clock.at, pane_launch.SHELL_PROMPT_S + pane_launch.SHELL_BACK_S + pane_launch.STARTUP_S)

    def test_no_tick_after_live_stands_still_at_all(self):
        clock, _ = self.stand()
        self.fake.screens[PANE] = 'not a screen anyone can read'
        for _ in range(6):
            before = clock.at
            self.later()
            self.assertEqual(clock.at, before)
        self.assertEqual(self.row_now()['state'], 'failed')

    def test_nor_while_the_first_turn_is_awaited(self):
        clock, _ = self.stand()
        for _ in range(4):
            before = clock.at
            self.later()
            self.assertEqual(clock.at, before)
        self.assertEqual(len(self.briefs()), 1)

    def test_the_launch_returns_at_live_with_no_brief_sent(self):
        self.up()
        self.assertEqual(self.briefs(), [])
        self.assertIsNone(self.brief_write())


class Brief(Case):
    def test_the_next_tick_sends_it_and_a_working_read_proves_the_turn(self):
        self.up()
        self.turns()
        self.later()
        self.assertEqual(len(self.briefs()), 1)
        self.assertEqual(self.brief_write()['state'], 'observed')
        self.later()
        self.assertEqual(len(self.briefs()), 1, 'once')

    def test_a_slow_turn_is_seen_on_a_later_tick(self):
        self.up()
        self.later()
        self.assertEqual(self.brief_write()['state'], 'sent')
        self.working()
        self.later()
        self.assertEqual(self.brief_write()['state'], 'observed')
        self.assertEqual(self.row_now()['state'], 'live')

    def test_an_unproven_turn_past_its_deadline_fails_the_row(self):
        self.up()
        self.later(0)
        self.later(pane_launch.FIRST_TURN_S - 5)
        self.assertEqual(self.row_now()['state'], 'live')
        self.later(6)
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual(self.reason(), 'first-turn-unproven')

    def test_a_short_first_turn_is_proved_by_the_ledger(self):
        self.up()
        self.later()
        self.notifications.acquire(ROLE, self.spec_['session_id'])
        self.later(pane_launch.FIRST_TURN_S + 1)
        self.assertEqual(self.brief_write()['state'], 'observed')
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_session_that_committed_has_had_its_turn(self):
        self.up()
        self.assertFalse(ops_panes.session_acted(self.conn, self.spec_['session_id']))
        event = self.port.append_event('director.processed', {}, actor='test')
        self.conn.execute('INSERT INTO director_processing(role_id,request_key,request_hash,event_id,receipt,generation,session_id,'
                          'lease_hash) VALUES(?,?,?,?,?,?,?,?)', (ROLE, 'k', 'h', event, '{}', 1, self.spec_['session_id'], 'l'))
        self.assertTrue(ops_panes.session_acted(self.conn, self.spec_['session_id']))
        self.assertFalse(ops_panes.session_acted(self.conn, 'another-session'))

    def test_a_pane_not_ready_past_its_deadline_fails_the_row_with_the_reason(self):
        self.up()
        self.fake.screens[PANE] = 'not a screen anyone can read'
        self.later(pane_launch.STARTUP_S - 5)
        self.assertEqual(self.row_now()['state'], 'live')
        self.later(6)
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual(self.briefs(), [])

    def test_a_pane_that_needs_the_owner_fails_at_once(self):
        self.up()
        self.fake.screens[PANE] = 'Allow once?\n❯ 1. Allow once\n  2. Deny\n'
        self.later()
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual(self.reason(), 'dialog')

    def test_a_write_left_pending_by_a_dead_hub_is_not_sent_again(self):
        self.up()
        ops_panes.note_write(self.conn, self.row['id'], 'brief', 'Drover: you are director role')
        self.later()
        self.assertEqual(self.briefs(), [])
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual(self.reason(), 'brief-unverified')

    def test_stop_before_the_brief_keeps_it_from_going(self):
        self.up()
        self.notifications.stop('owner', 'stop')
        self.later()
        self.later()
        self.assertEqual(self.briefs(), [])
        self.assertTrue(self.runtime.active_runs()[0]['stop_action_id'])

    def test_a_guard_that_fails_at_the_send_keeps_it_from_going(self):
        self.up()
        calls = []

        def refused():
            calls.append(1)
            if len(calls) > 1:
                raise AdmissionHeld('stop-work')
        self.panes._keep(self.runtime.active_runs()[0], refused)
        self.assertEqual(self.briefs(), [])
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_pause_keeps_the_brief_and_the_row(self):
        self.up()
        self.port.set_setting('dispatch_enabled', 'false')
        self.later(pane_launch.STARTUP_S + 1)
        self.assertEqual(self.briefs(), [])
        self.assertEqual(self.row_now()['state'], 'live')
        self.port.set_setting('dispatch_enabled', 'true')
        self.turns()
        self.later()
        self.assertEqual(len(self.briefs()), 1)

    def test_herdr_closed_past_every_deadline_ends_nothing(self):
        self.up()
        self.later()
        self.fake.closed = True
        self.later(pane_launch.FIRST_TURN_S + pane_launch.STARTUP_S)
        self.assertEqual(self.row_now()['state'], 'live')
        self.fake.closed = False
        self.working()
        self.later()
        self.assertEqual(self.brief_write()['state'], 'observed')

    def test_a_pane_that_will_not_read_past_every_deadline_ends_nothing(self):
        self.up()
        del self.fake.panes[PANE]['terminal_id']
        self.later(pane_launch.STARTUP_S + 1)
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(self.briefs(), [], 'an unknown read grants no write')

    def advance(self, seconds):
        return pane_launch.advance(self.port, self.herdr, self.row_now(), self.spec_, lambda: None, self.time + seconds, cfg=self.cfg)

    def test_herdr_closing_at_the_send_ends_nothing(self):
        self.up()
        self.fake.closed = True
        self.assertFalse(self.advance(pane_launch.STARTUP_S + 1))
        self.assertEqual(self.row_now()['state'], 'live')

    def test_herdr_closing_at_the_first_turn_ends_nothing(self):
        self.up()
        self.later()
        self.fake.closed = True
        self.assertFalse(self.advance(pane_launch.FIRST_TURN_S + 1))
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_brief_that_never_went_out_goes_again(self):
        self.up()
        write = ops_panes.note_write(self.conn, self.row['id'], 'brief', 'Drover: you are director role')
        ops_panes.finish_write(self.conn, write['id'], 'unsent', 'screen-changed')
        self.turns()
        self.later()
        self.assertEqual(len(self.briefs()), 1)

    def test_no_hook_runs_for_an_agent_that_left(self):
        self.up()
        self.turns()
        self.later()
        wake, handoff = self.hooks()
        del self.fake.agents[PANE]
        self.fake.processes = LONE_SHELL
        self.later()
        wake.assert_not_called()
        handoff.assert_not_called()

    def test_no_hook_runs_before_the_first_turn_is_proved(self):
        self.up()
        wake, handoff = self.hooks()
        self.later()
        wake.assert_not_called()
        self.working()
        self.later()
        wake.assert_called_once()
        handoff.assert_called_once()

    def test_an_agent_that_exits_before_its_brief_fails_the_row(self):
        self.up()
        del self.fake.agents[PANE]
        self.fake.processes = LONE_SHELL
        self.later(pane_launch.STARTUP_S + 1)
        self.assertEqual(self.row_now()['state'], 'failed')

    def test_a_footer_is_read_only_off_a_working_screen(self):
        self.up()
        self.later()
        with mock.patch.object(pane_launch, '_check_footer') as footer:
            self.notifications.acquire(ROLE, self.spec_['session_id'])
            self.later()
        footer.assert_not_called()

    def test_a_failed_first_turn_counts_as_a_launch_failure(self):
        self.up()
        self.later()
        self.later(pane_launch.FIRST_TURN_S + 1)
        del self.fake.agents[PANE]
        self.fake.processes = LONE_SHELL
        self.later(pane_launch.STARTUP_S + 1)
        self.assertEqual(self.outcome(), 'launch-failed')

    def test_interrupt_can_still_reach_a_pane_waiting_on_its_brief(self):
        self.up()
        self.fake.screens[PANE] = CLAUDE_IDLE
        self.assertTrue(pane_send.interrupt(self.port, self.herdr, self.row))


if __name__ == '__main__':
    unittest.main()
