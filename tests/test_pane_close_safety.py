"""Fix round 1 for pane_close: a new session, a queue, the real process snapshot, late closes and one pane's failure."""
import json
import subprocess
import unittest
from unittest import mock

from drover import pane_close
from drover.ledger import ops_panes
from tests.pane_close_fixtures import CLAUDE_READY, NOW, REPO, Fleet, PaneCloseBase, agent, snapshot

CODEX_QUEUED = ('Work summary.\n\nQueued follow-up inputs\n  ↳ please also fix the login bug\n'
                '    shift+← edit last queued message\n\n› \n')


class Session(PaneCloseBase):
    def test_a_new_session_in_the_recorded_terminal_keeps_the_pane(self):
        self.retired(session_id='drover-session')
        herdr = self.herdr({'w1:p1': agent('w1:p1', agent_session={'value': 'owner-new-session'})})
        self.tick(herdr, 0)
        result = self.tick(herdr, 120, apply=True)[0]
        self.assertEqual((result['verdict'], self.fake.closes()), ('keep', []))

    def test_a_missing_session_keeps_the_pane(self):
        for label, row_session, live in (('row', None, {'value': 's1'}), ('live', 's1', None), ('both', None, None),
                                         ('shape', 's1', 'string')):
            with self.subTest(label):
                self.conn.execute('DELETE FROM pane_agents')
                self.retired(session_id=row_session)
                herdr = self.herdr({'w1:p1': agent('w1:p1', agent_session=live)})
                self.tick(herdr, 0)
                self.assertEqual(self.tick(herdr, 120, apply=True)[0]['verdict'], 'keep')
                self.assertEqual(self.fake.closes(), [])

    def test_unknown_identity_keeps_the_pane(self):
        for label, change in (('kind', dict(agent=None)), ('seq', dict(state_change_seq=None)),
                              ('revision', dict(revision=None))):
            with self.subTest(label):
                self.conn.execute('DELETE FROM pane_agents')
                self.retired()
                herdr = self.herdr({'w1:p1': agent('w1:p1', **change)})
                self.tick(herdr, 0)
                self.assertEqual(self.tick(herdr, 120, apply=True)[0]['verdict'], 'keep')


class Queue(PaneCloseBase):
    def test_a_queued_follow_up_keeps_the_pane(self):
        self.retired(kind='codex')
        herdr = self.herdr({'w1:p1': agent('w1:p1', kind='codex')}, {'w1:p1': CODEX_QUEUED})
        self.tick(herdr, 0)
        result = self.tick(herdr, 120, apply=True)[0]
        self.assertEqual((result['verdict'], self.fake.closes()), ('keep', []))
        self.assertIn('queued', result['reason'])

    def test_a_screen_the_guards_cannot_read_keeps_the_pane(self):
        self.retired()
        herdr = self.herdr(screens={'w1:p1': CLAUDE_READY + '�'})
        self.tick(herdr, 0)
        self.assertEqual(self.tick(herdr, 120, apply=True)[0]['verdict'], 'keep')
        self.assertEqual(self.fake.closes(), [])


class RealSnapshot(PaneCloseBase):
    """A non-elevated listing always holds a process whose owner and cwd cannot be read (csrss, dwm)."""

    def test_the_real_shape_is_unknown_and_keeps_the_pane(self):
        proof = snapshot((101, REPO, 100), system=True)(None)
        self.assertTrue(proof.unknown)
        self.assertIn('csrss.exe', [row['name'] for row in proof.unreadable])
        self.retired()
        herdr = self.herdr()
        with mock.patch.object(pane_close.placement, 'cleanup_processes', return_value=proof), \
                mock.patch.object(pane_close.placement, 'cleanup_side', return_value='windows'):
            self.tick(herdr, 0, processes=None)
            result = self.tick(herdr, 120, processes=None, apply=True)[0]
        self.assertEqual((result['verdict'], self.fake.closes()), ('keep', []))
        self.assertIn('visibility unavailable', result['reason'])

    def test_the_default_snapshot_is_the_placement_proof_of_the_hub_side(self):
        self.retired()
        herdr = self.herdr()
        proof = snapshot((101, REPO, 100))(None)
        with mock.patch.object(pane_close.placement, 'cleanup_processes', return_value=proof) as taken, \
                mock.patch.object(pane_close.placement, 'cleanup_side', return_value='windows'):
            self.tick(herdr, 0, processes=None)
            self.assertEqual(self.tick(herdr, 60, processes=None)[0]['verdict'], 'close')
        taken.assert_called()


class OwnTree(PaneCloseBase):
    def test_the_panes_descendants_never_block_it(self):
        self.processes = snapshot((101, REPO, 100), (102, REPO, 101), (103, REPO, 102))
        self.retired()
        herdr = self.herdr()
        self.tick(herdr, 0)
        self.assertEqual(self.tick(herdr, 60)[0]['verdict'], 'close')

    def test_a_process_beside_the_pane_still_blocks_it(self):
        self.processes = snapshot((101, REPO, 100), (102, REPO, 101), (4242, REPO, 100 + 1000))
        self.retired()
        herdr = self.herdr()
        self.tick(herdr, 0)
        self.assertIn('live process', self.tick(herdr, 60)[0]['reason'])

    def test_an_unknown_tree_keeps_the_pane(self):
        self.processes = snapshot((101, REPO, 100), (102, REPO, None))
        self.retired()
        herdr = self.herdr()
        self.tick(herdr, 0)
        result = self.tick(herdr, 60, apply=True)[0]
        self.assertEqual((result['verdict'], self.fake.closes()), ('keep', []))
        self.assertIn('visibility unavailable', result['reason'])


class LateClose(PaneCloseBase):
    def late(self, closes=True):
        """A Fleet whose `pane close` takes effect (or not) and then times out."""
        herdr = self.herdr()
        fake = self.fake

        def run(argv, **kwargs):
            if argv[1:3] == ['pane', 'close']:
                if closes:
                    fake._close(list(argv[1:]))
                fake.calls.append(list(argv[1:]))
                raise subprocess.TimeoutExpired(argv, 1)
            return Fleet.__call__(fake, argv, **kwargs)

        herdr.run = run
        return herdr

    def test_a_close_that_times_out_is_settled_once_on_the_next_tick(self):
        row = self.retired()
        herdr = self.late()
        self.tick(herdr, 0)
        first = self.tick(herdr, 60, apply=True)[0]
        self.assertEqual(first['verdict'], 'keep')
        self.assertEqual(self.events('pane-closed') + self.events('pane-close-failed'), [])
        herdr.clock = lambda: 1e9       # the retry window of the client has passed
        second = self.tick(herdr, 61, apply=True)[0]
        self.assertEqual(second['verdict'], 'closed')
        self.assertEqual(len(self.events('pane-closed')), 1)
        self.assertEqual(self.events('pane-close-failed'), [])
        self.assertEqual(self.conn.execute('SELECT state FROM pane_agents WHERE id=?', (row['id'],)).fetchone()[0], 'gone')
        self.assertEqual(self.tick(herdr, 62, apply=True), [])
        self.assertEqual(len(self.events('pane-closed')), 1)

    def test_a_close_that_never_happened_is_one_failure_and_the_pane_is_judged_again(self):
        self.retired()
        herdr = self.late(closes=False)
        self.tick(herdr, 0)
        self.tick(herdr, 60, apply=True)
        herdr.clock = lambda: 1e9
        self.assertEqual(self.tick(herdr, 61, apply=True)[0]['verdict'], 'close-failed')
        self.assertEqual(len(self.events('pane-close-failed')), 1)
        self.assertEqual(self.events('pane-closed'), [])
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['state'], 'retired')

    def test_herdr_still_down_leaves_the_close_pending(self):
        self.retired()
        herdr = self.late()
        self.tick(herdr, 0)
        self.tick(herdr, 60, apply=True)
        self.fake.closed = True
        herdr.run = self.fake
        herdr.clock = lambda: 1e9
        self.assertEqual(self.tick(herdr, 61, apply=True)[0]['verdict'], 'keep')
        self.assertEqual(self.events('pane-closed') + self.events('pane-close-failed'), [])


class Isolation(PaneCloseBase):
    def two(self):
        self.retired()
        self.retired(pane='w1:p2')
        self.pids['w1:p2'] = dict(shell_pid=100, foreground_processes=[dict(pid=101)])
        return self.herdr({p: agent(p) for p in ('w1:p1', 'w1:p2')})

    def test_a_stuck_checkout_keeps_its_pane_and_judges_the_rest(self):
        herdr = self.two()
        seen = []

        def run(cwd, *args):
            seen.append(cwd)
            if len(seen) == 1:
                raise subprocess.TimeoutExpired('git', 30)
            return self.git(cwd, *args)

        results = self.tick(herdr, 0, run=run)
        self.assertEqual([r['verdict'] for r in results], ['keep', 'wait'])

    def test_an_unexpected_error_in_one_pane_is_kept_and_logged(self):
        herdr = self.two()
        calls = []
        ok = self.processes

        def processes(row):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('boom')
            return ok(row)

        with self.assertLogs('drover.pane_close', 'WARNING') as logged:
            results = self.tick(herdr, 0, processes=processes)
        self.assertEqual([r['verdict'] for r in results], ['keep', 'wait'])
        self.assertIn('RuntimeError', logged.output[0])

    def test_a_bad_clock_setting_restarts_the_clocks(self):
        self.retired()
        herdr = self.herdr()
        for bad in ('[]', '5', '{"w1:p1": 5}', 'null', 'nonsense'):
            with self.subTest(bad):
                self.port.set_setting(pane_close.SETTING, bad)
                self.port.set_setting(pane_close.PENDING, bad)
                self.assertEqual(self.tick(herdr, 0)[0]['verdict'], 'wait')

    def test_a_malformed_agent_is_a_keep(self):
        self.retired()
        for bad in (None, 'text', 5, []):
            with self.subTest(bad):
                herdr = self.herdr({'w1:p1': agent('w1:p1')})
                self.fake.agents['w1:p1'] = bad
                self.fake._agent = lambda args, bad=bad: (0, json.dumps({'result': {'agent': bad}}), '')
                self.assertEqual(self.tick(herdr, 0)[0]['verdict'], 'keep')

    def test_a_row_that_goes_gone_during_the_tick_is_skipped(self):
        self.retired()
        herdr = self.herdr()
        with mock.patch.object(ops_panes, 'by_pane', return_value=None):
            self.assertEqual(self.tick(herdr, 0), [])
        self.assertEqual(self.fake.calls, [])


if __name__ == '__main__':
    unittest.main()
