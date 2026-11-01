"""Launches that ended badly: a `starting` row after a hub death, and the `failed` panes left behind."""
import unittest
from unittest import mock

from drover import pane_close, pane_launch
from drover.ledger import ops_panes
from tests.pane_launch_support import LONE_SHELL
from tests.pane_send_support import PANE
from tests.panes_support import ROLE, Resident


class DeadLaunches(Resident):
    def past_deadline(self):
        self.time += pane_launch.START_DEADLINE_S + 1

    def test_a_starting_row_within_its_deadline_is_left_alone(self):
        self.died_starting()
        self.assertEqual(self.adapter.probe(self.runtime.active_runs()[0]['spec']), 'unknown')
        self.tick()
        self.assertEqual(self.row_now()['state'], 'starting')
        self.assertEqual(self.port.events(kind='pane-launch-failed'), [])

    def test_a_starting_row_past_its_deadline_ends_failed_after_a_hub_death(self):
        self.died_starting()
        self.fake.processes = LONE_SHELL
        self.past_deadline()
        self.adapter.probe(self.runtime.active_runs()[0]['spec'])
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual(self.reason(), 'launch-deadline')
        self.assertEqual(len(self.notices()), 1)

    def test_its_role_is_freed_when_the_pane_holds_no_agent(self):
        self.died_starting()
        self.fake.processes = LONE_SHELL
        self.past_deadline()
        self.tick()
        self.assertEqual(self.outcome(), 'launch-failed')
        self.assertEqual(self.row_now()['state'], 'retired')
        self.assertNotIn(self.rid, [r['run_id'] for r in self.runtime.active_runs()])
        self.assertEqual(self.role()['replacements_used'], 1)
        self.assertEqual(len(self.calls('tab', 'create')), 1, 'the first launch opened none: it died in the test')

    def test_its_pane_retires_when_no_wake_is_waiting(self):
        self.died_starting()
        self.fake.processes = LONE_SHELL
        self.past_deadline()
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        self.conn.commit()
        with mock.patch.object(pane_close, 'tick', return_value=[]):
            self.tick()
            self.tick()
        self.assertEqual(self.outcome(), 'launch-failed')
        self.assertEqual(self.row_now()['state'], 'retired')

    def test_its_role_is_freed_when_the_pane_is_gone(self):
        self.died_starting()
        del self.fake.panes[PANE]
        self.tick()
        self.assertEqual(self.outcome(), 'pane-gone')
        self.assertEqual(self.row_now()['state'], 'gone')

    def test_its_role_stays_held_while_an_agent_is_in_the_pane(self):
        self.died_starting()
        self.agent_in_pane()
        self.past_deadline()
        self.tick()
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual([r['run_id'] for r in self.runtime.active_runs()], [self.rid])
        self.assertEqual(self.role()['replacements_used'], 0)
        self.fake.processes = LONE_SHELL
        self.tick()
        self.assertEqual(self.outcome(), 'launch-failed')
        self.assertNotIn(self.rid, [r['run_id'] for r in self.runtime.active_runs()])

    def test_herdr_closed_changes_no_starting_row(self):
        self.died_starting()
        self.past_deadline()
        self.fake.closed = True
        self.tick()
        self.assertEqual(self.row_now()['state'], 'starting')
        self.assertEqual(self.port.events(kind='pane-launch-failed'), [])
        self.fake.closed = False
        self.fake.processes = LONE_SHELL
        self.tick()
        self.assertEqual(self.port.events(kind='pane-launch-failed')[0]['data']['reason'], 'launch-deadline')

    def test_any_other_refusal_changes_no_starting_row(self):
        self.died_starting()
        self.past_deadline()
        self.fake.refuse[('pane', 'get')] = 'internal_error'
        self.tick()
        self.assertEqual(self.row_now()['state'], 'starting')

    def test_a_starting_row_with_no_run_is_still_reconciled(self):
        self.build()
        self.fake.panes[PANE] = dict(pane_id=PANE, terminal_id='t1')
        self.fake.processes = LONE_SHELL
        self.row = ops_panes.record_agent(self.conn, pane_id=PANE, terminal_id='t1', kind='claude', seat='s', placement='windows',
                                          started_by='drover', role_id=ROLE, run_id=404, cwd=str(self.cwd), name='n')
        self.past_deadline()
        self.tick()
        self.assertEqual(self.row_now()['state'], 'retired')


class FailedPanes(Resident):
    def failed(self, *, boots=False):
        """A launch that failed in this hub, its pane left behind."""
        self.build(boots=boots)
        self.wake()
        self.rid, = self.tick()['launched']
        self.row = ops_panes.by_run(self.conn, self.rid)
        self.assertEqual(self.row['state'], 'failed')
        return self.row

    def settled(self):
        self.time += pane_launch.STARTUP_S + 1

    def test_a_failed_pane_with_no_agent_is_retired(self):
        self.failed()
        self.settled()
        self.tick()
        self.assertEqual(self.outcome(), 'launch-failed')
        self.assertEqual(self.row_now()['state'], 'retired')
        self.assertEqual(self.port.events(kind='pane-retired')[-1]['data']['pane_id'], PANE)

    def test_one_read_of_a_lone_shell_right_after_a_failure_proves_nothing(self):
        self.failed()
        self.tick()
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual([r['run_id'] for r in self.runtime.active_runs()], [self.rid])

    def test_a_failed_pane_with_an_agent_keeps_its_row_and_its_role(self):
        self.failed()
        self.agent_in_pane()
        self.settled()
        self.wake()
        self.tick()
        self.assertEqual(self.row_now()['state'], 'failed')
        self.assertEqual([r['run_id'] for r in self.runtime.active_runs()], [self.rid])
        self.assertEqual(len(self.calls('tab', 'create')), 1)

    def test_a_failed_pane_the_owner_closed_is_gone_and_clean(self):
        self.failed()
        del self.fake.panes[PANE]
        self.tick()
        self.assertEqual(self.row_now()['state'], 'gone')
        self.assertEqual(self.outcome(), 'pane-gone')
        self.assertEqual(self.role()['replacements_used'], 0)

    def test_a_failed_pane_whose_terminal_changed_is_gone(self):
        self.failed()
        self.fake.panes[PANE]['terminal_id'] = 't9'
        self.tick()
        self.assertEqual(self.row_now()['state'], 'gone')

    def test_a_failed_launch_replaces_itself_once_then_holds(self):
        self.failed()
        self.settled()
        self.wake()
        self.tick()
        self.assertEqual(self.role()['replacements_used'], 1)
        self.assertEqual(len(self.calls('tab', 'create')), 2)
        second = self.runtime.active_runs()[0]
        self.settled()
        self.wake()
        result = self.tick()
        self.assertEqual(self.outcome(second['run_id']), 'launch-failed')
        self.assertIn('replacement-budget-exhausted', str(result['held']))
        self.assertEqual(len(self.calls('tab', 'create')), 2)

    def test_a_retired_agent_free_pane_waits_for_a_ruling_on_pane_close(self):
        """Pins today's gap: `pane_close` needs an agent report, so this pane is kept, and the report asks for a ruling."""
        self.failed()
        self.settled()
        self.tick()
        result = pane_close.tick(self.port, self.herdr, self.time, apply=True, cfg=self.cfg)
        self.assertEqual([r['verdict'] for r in result], ['keep'])
        self.assertIn(PANE, self.fake.panes)

    def test_owner_rows_are_never_reconciled(self):
        self.build()
        self.fake.processes = LONE_SHELL
        self.row = ops_panes.record_agent(self.conn, pane_id=PANE, terminal_id='t1', kind='claude', seat='s', placement='windows',
                                          started_by='owner', state='failed', role_id=ROLE, cwd=str(self.cwd))
        self.fake.panes[PANE] = dict(pane_id=PANE, terminal_id='t1')
        self.settled()
        self.panes.sweep()
        self.assertEqual(self.row_now()['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
