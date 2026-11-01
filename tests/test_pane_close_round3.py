"""Fix round 3: a process that has exited cannot be using a checkout, and one close is one event."""
import unittest
from unittest import mock

from drover import pane_close, placement_proof
from drover.ledger import ops_panes
from tests.pane_close_fixtures import REPO, PaneCloseBase


def listing(*extra):
    """What a busy host gives: a readable pane process, and processes whose owner and cwd could not be read."""
    rows = [dict(pid=101, name='node.exe', same_user=True, cwd=REPO, cmd=['node'], ppid=100)]
    for pid, name in extra:
        rows.append(dict(pid=pid, name=name, same_user=None, cwd=None, cmd='', session_id=1, ppid=1, exe=None))
    return rows


def proof(rows, alive, **request):
    request = dict(paths={'process_ignore': []}, **request)
    with mock.patch.object(placement_proof, '_windows_processes', side_effect=alive) as taken:
        result = placement_proof._CleanupProcesses(windows=True, cim=lambda: rows, request=request)
    return result, taken


class Exited(unittest.TestCase):
    def test_a_row_whose_process_is_gone_is_dropped(self):
        result, taken = proof(listing((7001, 'git.exe'), (7002, 'sleep.exe')), [{'101': ['node.exe', 1]}],
                              clear_ignored=True)
        self.assertFalse(result.unknown)
        self.assertEqual(result.unreadable, [])
        taken.assert_called_once()

    def test_a_row_that_still_exists_stays_unknown(self):
        result, _ = proof(listing((7001, 'git.exe'), (7002, 'sleep.exe')),
                          [{'101': ['node.exe', 1], '7002': ['sleep.exe', 3]}], clear_ignored=True)
        self.assertTrue(result.unknown)
        self.assertEqual([row['name'] for row in result.unreadable], ['sleep.exe'])

    def test_a_fresh_list_that_fails_keeps_every_row(self):
        for error in (OSError('no snapshot'), ValueError('bad')):
            with self.subTest(error):
                result, _ = proof(listing((7001, 'git.exe')), error, clear_ignored=True)
                self.assertTrue(result.unknown)
                self.assertEqual(len(result.unreadable), 1)

    def test_without_the_key_nothing_is_rechecked(self):
        result, taken = proof(listing((7001, 'git.exe')), [{}])
        self.assertTrue(result.unknown)
        taken.assert_not_called()

    def real_path(self, answer, alive):
        """The real route (no injected listing): a native list, then the CIM answer, then the fresh list."""
        request = dict(paths={'process_ignore': []}, clear_ignored=True)
        with mock.patch.object(placement_proof, '_windows_processes', side_effect=alive),                 mock.patch.object(placement_proof, '_cleanup_cim', return_value=answer),                 mock.patch.object(placement_proof, '_cim_rows', return_value=[]):
            return placement_proof._CleanupProcesses(windows=True, request=request)

    def test_a_failed_query_is_never_cleared(self):
        born = ['git.exe', 5]
        row = dict(pid=7001, name='git.exe', same_user=None, cwd=None, cmd='', session_id=1, ppid=1, exe=None)
        answer = dict(rows=[row], identities={'7001': born}, reason_code='process_visibility_unavailable')
        result = self.real_path(answer, [{'7001': born}, {}])      # the row has exited by the fresh list
        self.assertTrue(result.rows)
        self.assertTrue(result.query_failure)
        self.assertTrue(result.unknown)
        self.assertEqual([r['pid'] for r in result.unreadable], [7001])

    def test_a_snapshot_that_listed_nothing_is_never_cleared(self):
        answer = dict(rows=[], identities={})
        result = self.real_path(answer, [{'7001': ['git.exe', None]}, {}, {}])
        self.assertEqual(result.rows, [])
        self.assertTrue(result.unreadable)
        self.assertTrue(result.unknown)

    def test_a_new_creation_time_drops_the_row_and_an_unknown_one_keeps_it(self):
        result, _ = proof(listing((7001, 'git.exe'), (7002, 'git.exe'), (7003, 'git.exe')), [{'101': ['node.exe', 1]}],
                          clear_ignored=True)
        result.unreadable = [dict(pid=7001), dict(pid=7002), dict(pid=7003), dict(pid=7004)]
        result.current = {'7001': ['git.exe', 5], '7002': ['git.exe', 5], '7003': ['git.exe', None],
                          '7004': ['git.exe', 5]}
        result._drop_exited({'7001': ['git.exe', 9], '7002': ['git.exe', 5], '7003': ['git.exe', 9],
                             '7004': ['git.exe', None]})
        self.assertEqual([row['pid'] for row in result.unreadable], [7002, 7003, 7004])
        self.assertTrue(result.unknown)


class OneEvent(PaneCloseBase):
    def test_a_failure_between_the_event_and_the_row_leaves_no_event_and_the_next_tick_writes_one(self):
        row = self.retired()
        herdr = self.herdr()
        self.tick(herdr, 0)
        real = ops_panes.update_agent
        with mock.patch.object(ops_panes, 'update_agent', side_effect=RuntimeError('database is locked')):
            first = self.tick(herdr, 60, apply=True)[0]
        self.assertEqual(first['verdict'], 'keep')
        self.assertEqual(self.events('pane-closed'), [])
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['state'], 'retired')
        second = self.tick(herdr, 61, apply=True)[0]
        self.assertEqual(second['verdict'], 'closed')
        self.assertEqual(len(self.events('pane-closed')), 1)
        self.assertEqual(self.conn.execute('SELECT state FROM pane_agents WHERE id=?', (row['id'],)).fetchone()[0],
                         'gone')
        self.assertIs(ops_panes.update_agent, real)
        self.assertEqual(pane_close._load(self.port, pane_close.PENDING), {})


if __name__ == '__main__':
    unittest.main()
