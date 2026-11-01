"""The ways out of a pane: `close_pane` is the one `pane close`, the owner's switch rules it, and the runbook's rollback ends a run."""
from unittest import mock

from drover import pane_close, pane_send
from drover.directors import RunnerDirectorAdapter
from drover.herdr_client import HerdrRefused
from drover.ledger import ops_panes
from drover.pane_send import PaneRefused
from tests.pane_launch_support import LONE_SHELL, Launching, PROMPT
from tests.pane_send_support import PANE
from tests.panes_support import Resident


class Switch(Launching):
    def test_it_is_off_by_default_and_reads_the_owner_values(self):
        self.scene()
        self.assertIs(pane_send.close_enabled(self.port), False)
        for value, wanted in (('true', True), ('ON', True), ('1', True), ('no', False), ('', False)):
            with self.subTest(value):
                self.port.set_setting('pane_close_enabled', value)
                self.assertIs(pane_send.close_enabled(self.port), wanted)


class BlankTab(Launching):
    """No text of the spec exempts a blank tab, and plan row 19 and ruling 23 make every close wait on the owner: off, it stays."""

    def blank(self):
        fake = self.scene()
        fake.screens[PANE] = ''
        return fake

    def test_with_closes_off_the_failed_launch_leaves_its_tab(self):
        self.blank()
        row = self.start()
        self.assertEqual((row['state'], self.calls('pane', 'close')), ('failed', []))
        self.assertEqual(self.reason(), 'shell-not-ready')
        self.assertIn('Closing it frees the role.', self.notices()[0]['body'])

    def test_with_closes_on_it_closes(self):
        self.port.set_setting('pane_close_enabled', 'true')
        self.blank()
        self.assertEqual(self.start()['state'], 'gone')
        self.assertEqual(self.calls('pane', 'close'), [['pane', 'close', PANE]])


class ClosePane(Launching):
    def setUp(self):
        super().setUp()
        self.scene()

    def row(self, **over):
        return ops_panes.record_agent(self.conn, **{'pane_id': PANE, 'terminal_id': 't1', 'kind': 'claude', 'seat': 'seat-c',
                                                    'placement': 'windows', 'started_by': 'drover', **over})

    def test_a_retired_row_or_a_starting_one_closes_by_id(self):
        for state in ('retired', 'starting'):
            with self.subTest(state):
                self.fake.panes[PANE] = dict(pane_id=PANE)
                pane_send.close_pane(self.port, self.herdr, self.row(state=state))
                self.assertEqual(self.calls('pane', 'close')[-1], ['pane', 'close', PANE])
                self.conn.execute('DELETE FROM pane_agents')

    def test_a_running_agents_pane_never_closes(self):
        for state in ('live', 'rotating', 'compaction', 'failed'):
            with self.subTest(state), self.assertRaises(PaneRefused):
                pane_send.close_pane(self.port, self.herdr, self.row(state=state))
            self.conn.execute('DELETE FROM pane_agents')
        self.assertEqual(self.calls('pane', 'close'), [])

    def test_an_owners_row_or_a_stale_copy_never_closes(self):
        row = self.row(state='retired')
        ops_panes.update_agent(self.conn, row['id'], state='gone')
        with self.assertRaises(PaneRefused):
            pane_send.close_pane(self.port, self.herdr, row)
        with self.assertRaises(PaneRefused):
            pane_send.close_pane(self.port, self.herdr, self.row(state='retired', started_by='owner'))
        self.assertEqual(self.calls('pane', 'close'), [])

    def test_herdr_errors_reach_the_caller_unchanged(self):
        row = self.row(state='retired')
        self.fake.refuse[('pane', 'close')] = 'pane_busy'
        with self.assertRaises(HerdrRefused) as caught:
            pane_send.close_pane(self.port, self.herdr, row)
        self.assertEqual(caught.exception.code, 'pane_busy')

    def test_a_refusal_at_the_last_moment_settles_the_intent(self):
        row = self.row(state='retired')
        entry = dict(pane=PANE, fingerprint=None, idle_s=1, worktree=None, reason='r', verdict='close')
        pending = {}
        with mock.patch.object(pane_close, 'assess', return_value=entry), \
                mock.patch.object(pane_send, 'close_pane', side_effect=PaneRefused('not-live')):
            result = pane_close.close_if_still_eligible(self.port, self.herdr, row, entry, now=0, observed={}, pending=pending)
        self.assertEqual((result['verdict'], pending), ('keep', {}))


class Rollback(Resident):
    """docs/operations/directors-rollout.md, in its order."""

    def open_run(self):
        return self.conn.execute("SELECT COUNT(*) FROM director_runs WHERE state<>'finished'").fetchone()[0]

    def slots(self):
        return self.conn.execute("SELECT COUNT(*) FROM runs WHERE ended IS NULL AND role<>'gate'").fetchone()[0]

    def disabled(self):
        self.launched()
        self.port.set_setting('director_execution_enabled', 'false')

    def test_disabling_execution_alone_stops_nothing(self):
        self.disabled()
        self.tick()
        self.assertEqual((self.open_run(), self.row_now()['state']), (1, 'live'))

    def test_closing_the_pane_ends_the_run_and_frees_the_slot_for_good(self):
        self.disabled()
        self.herdr.call('pane', 'close', PANE)
        self.tick()
        self.assertEqual((self.open_run(), self.slots(), self.row_now()['state']), (0, 0, 'gone'))
        self.assertEqual(ops_panes.running_agents(self.conn), [])
        self.assertIsNone(self.role()['held_reason'])
        self.wake()
        self.assertEqual(self.tick()['launched'], [])

    def test_exit_alone_leaves_the_run_open(self):
        self.disabled()
        self.fake.agents.pop(PANE)
        self.fake.processes = LONE_SHELL
        self.fake.screens[PANE] = PROMPT
        self.tick()
        self.assertEqual(self.open_run(), 1)

    def test_herdr_closed_ends_nothing(self):
        self.disabled()
        self.fake.closed = True
        self.tick()
        self.assertEqual(self.open_run(), 1)

    def test_the_old_adapter_cannot_see_a_pane_run_end(self):
        self.disabled()
        launcher = mock.Mock(runs_dir=self.state)
        self.assertEqual(RunnerDirectorAdapter(launcher).probe(self.spec_), 'unknown')
