"""Archived tasks still participate in holds and duplicate detection."""
import tempfile
import unittest
from pathlib import Path

from drover import garden
from drover.ledger import ops
from hub.store import Store


class ArchiveRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(self.store.close)
        self.conn = self.store.conn

    def task(self, title, epic_id, status, scope_files):
        task = ops.create_task(self.conn, title, 'acme-lib', epic_id=epic_id, scope_files=scope_files)
        if status == 'ready':
            ops.approve(self.conn, task['id'])
        elif status == 'draft':
            ops.set_status(self.conn, task['id'], status)
        return task['id']

    def assert_held_after_restore(self, task_id, source, reason):
        task = ops.archive_task(self.conn, task_id, False)
        self.assertEqual(task['status'], 'blocked')
        self.assertIn({'source': source, 'reason': reason}, task['blocker']['holds'])
        self.assertIsNone(ops.claim_next(self.conn, {'owner_kind': 'claude', 'model': 'sonnet'}))

    def test_archived_overlapping_tasks_keep_replan_hold_when_restored(self):
        for status in ('proposed', 'draft', 'ready'):
            with self.subTest(status=status):
                epic = ops.create_epic(self.conn, 'acme-lib', 'Replan ' + status)['id']
                source = self.task('Source', epic, 'ready', ['src/*'])
                target = self.task('Overlap', epic, status, ['src/module.py'])
                ops.archive_task(self.conn, target)
                ops.replan_hold(self.conn, source)
                self.assert_held_after_restore(target, source, 'replan')
                ops.replan_release(self.conn, source)
                self.assertEqual(ops.get_task(self.conn, target)['status'], status)
                # Keep this subtest's tasks out of subsequent claim checks.
                ops.archive_task(self.conn, source)
                ops.archive_task(self.conn, target)

    def test_archived_pending_tasks_keep_epic_urgent_hold_when_restored(self):
        for status in ('proposed', 'draft', 'ready'):
            with self.subTest(status=status):
                epic = ops.create_epic(self.conn, 'acme-lib', 'Urgent ' + status)['id']
                target = self.task('Target', epic, status, ['src/*'])
                ops.archive_task(self.conn, target)
                ops.add_comment(self.conn, 'epic', epic, 'owner', 'Pause work', urgent=True)
                self.assert_held_after_restore(target, epic, 'urgent')
                ops.replan_release(self.conn, epic, reason='urgent')
                self.assertEqual(ops.get_task(self.conn, target)['status'], status)
                ops.archive_task(self.conn, target)

    def test_import_deduplicates_archived_tasks(self):
        items = [{'title': 'Update acme-lib', 'repo': 'acme-lib', 'epic': 'Maintenance'}]
        task_id, = self.store.import_tasks(items, dry_run=False)['created']
        self.store.archive_task(task_id)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = self.store.import_tasks(items, dry_run=dry_run)
                self.assertEqual(result['created'], [])
                self.assertEqual(result['counts'], {'create': 0, 'duplicate': 1, 'invalid': 0})
                self.assertEqual(self.store.list_tasks(), [])
                self.assertEqual([t['id'] for t in self.store.list_tasks(include_archived=True)], [task_id])
        self.store.archive_task(task_id, False)
        self.assertEqual([t['id'] for t in self.store.list_tasks()], [task_id])

    def test_branch_discovery_deduplicates_archived_tasks(self):
        repo = Path(self.tmp.name) / 'acme-lib'
        items = [{'branch': 'feature/update', 'reason': 'Useful work'}]
        task_id, = garden.make_tasks_for_valuable(self.conn, repo, items)
        ops.archive_task(self.conn, task_id)
        self.assertEqual(garden.make_tasks_for_valuable(self.conn, repo, items), [])
        self.assertEqual(ops.list_tasks(self.conn), [])
        self.assertEqual([t['id'] for t in ops.list_tasks(self.conn, include_archived=True)], [task_id])
        task = ops.archive_task(self.conn, task_id, False)
        self.assertEqual(task['branch'], 'feature/update')
        self.assertEqual([t['id'] for t in ops.list_tasks(self.conn)], [task_id])
