"""Accept findings through the same owner UI operation used by Approve."""
import tempfile
import unittest
from pathlib import Path

from drover.ledger import ops
from hub.store import Store, UiView


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.s = Store(Path(tmp.name) / 'ledger.db')
        self.addCleanup(self.s.close)
        # decide_item uses only the store, with no template/runtime dependency.
        self.ui = UiView.__new__(UiView)
        self.ui.s = self.s

    def finished_read(self, **fields):
        t = self.s.create_task('Findings', kind='research', **fields)['id']
        self.s.approve(t)
        self.s.set_status(t, 'claimed', 'dispatcher')
        rid = self.s.start_run(t, 'worker', 'codex')['id']
        self.s.finish_run(rid, 'read-only-done', note='Findings ready')
        self.s.set_status(t, 'needs-owner', 'dispatcher', 'Review findings')
        return t, rid

    def test_owner_accepts_findings_and_unblocks_dependents_without_landing(self):
        tid, rid = self.finished_read()
        dependent = self.s.create_task('Use findings', deps=[tid])['id']
        self.s.approve(dependent)
        self.assertEqual(ops.ready_tasks(self.s.conn), [])
        self.ui.decide_item('task', tid, 'approve')
        self.assertEqual(self.s.get_task(tid)['status'], 'landed')
        self.assertEqual([t['id'] for t in ops.ready_tasks(self.s.conn)], [dependent])
        self.assertEqual([r['id'] for r in self.s.list_runs(tid)], [rid])
        self.assertIsNone(self.s.get_task(tid)['branch'])
        self.assertIsNone(self.s.get_task(tid)['worktree'])
        event = self.s.list_events(task_id=tid, kind='status')[-1]
        self.assertEqual(event['actor'], 'owner')
        self.assertEqual(event['data']['reason'], 'Owner accepted read-only findings')
        # No landing metadata is created by acceptance.
        self.assertIsNone(self.s.conn.execute(
            "SELECT task_id FROM task_landed WHERE task_id=?", (tid,)).fetchone())

    def test_only_owner_may_complete_successful_read_work(self):
        tid, _ = self.finished_read()
        with self.assertRaises(PermissionError):
            ops.set_status(self.s.conn, tid, 'landed', actor='dispatcher')
        self.assertEqual(self.s.get_task(tid)['status'], 'needs-owner')

    def test_failed_or_unfinished_read_work_is_retried_on_approve(self):
        for role, outcome in (('worker', 'blocked'), ('worker', None), ('review', 'read-only-done')):
            with self.subTest(role=role, outcome=outcome):
                tid, _ = self.finished_read()
                rid = self.s.start_run(tid, role, 'codex')['id']
                if outcome:
                    self.s.finish_run(rid, outcome)
                with self.assertRaises(ValueError):
                    ops.set_status(self.s.conn, tid, 'landed', actor='owner')
                self.ui.decide_item('task', tid, 'approve')
                self.assertEqual(self.s.get_task(tid)['status'], 'ready')

    def test_write_task_cannot_take_read_acceptance_shortcut(self):
        tid, _ = self.finished_read(write=True)
        with self.assertRaises(ValueError):
            ops.set_status(self.s.conn, tid, 'landed', actor='owner')
        self.ui.decide_item('task', tid, 'approve')
        self.assertEqual(self.s.get_task(tid)['status'], 'ready')
