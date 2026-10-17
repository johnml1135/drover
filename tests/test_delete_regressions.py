"""Task deletion cleans up shared gate questions and holds atomically."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from drover import pipeline
from drover.ledger import connect, ops
from drover.port import DirectPort


class DeleteRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.pipe = pipeline.Pipeline(DirectPort(self.conn), repos_map={
            'acme-lib': {'name': 'acme-lib', 'gate': {'unknown': True}}}, runs_dir=self.tmp.name)

    def task(self, title, **kw):
        task = ops.create_task(self.conn, title, 'acme-lib', **kw)
        ops.approve(self.conn, task['id'])
        return task['id']

    def gate_question(self, task_id):
        self.assertFalse(self.pipe.gate_known(ops.get_task(self.conn, task_id)))
        return int(ops.get_setting(self.conn, '_gate_question_acme-lib'))

    def test_open_gate_question_moves_to_surviving_task(self):
        source, target = self.task('Source'), self.task('Target')
        private = ops.ask_question(self.conn, 'task', source, 'Task-specific question')
        qid = self.gate_question(source)
        self.gate_question(target)
        ops.delete_task(self.conn, source, confirm=True)
        self.assertEqual(self.pipe.sync_gates(), [])
        self.assertEqual(ops.get_question(self.conn, qid)['target_id'], target)
        with self.assertRaises(ops.NotFound):
            ops.get_question(self.conn, private)
        ops.answer_question(self.conn, qid, text='python3 -m unittest')
        self.pipe.sync_gates()
        self.assertEqual(self.pipe._owner_gate_cmd('acme-lib'), 'python3 -m unittest')
        self.assertEqual(ops.get_task(self.conn, target)['status'], 'ready')

    def test_answered_gate_keeps_owner_provenance_and_command(self):
        source, target = self.task('Source'), self.task('Target')
        qid = self.gate_question(source)
        ops.answer_question(self.conn, qid, text='python3 -m unittest')
        self.pipe.sync_gates()
        command = ops.get_setting(self.conn, 'gate_cmd_acme-lib')
        ops.archive_task(self.conn, target)
        ops.delete_task(self.conn, source, confirm=True)
        self.assertEqual(self.pipe.sync_gates(), [])
        self.assertEqual(ops.get_question(self.conn, qid)['target_id'], target)
        self.assertEqual(ops.get_setting(self.conn, 'gate_cmd_acme-lib'), command)
        self.assertEqual(self.pipe._owner_gate_cmd('acme-lib'), 'python3 -m unittest')

    def test_last_task_clears_gate_references_before_next_task(self):
        source = self.task('Source')
        qid = self.gate_question(source)
        ops.answer_question(self.conn, qid, text='python3 -m unittest')
        self.pipe.sync_gates()
        ops.delete_task(self.conn, source, confirm=True)
        self.assertIsNone(ops.get_setting(self.conn, '_gate_question_acme-lib'))
        self.assertIsNone(ops.get_setting(self.conn, 'gate_cmd_acme-lib'))
        self.assertEqual(self.pipe.sync_gates(), [])
        new_qid = self.gate_question(self.task('New work'))
        self.assertNotEqual(new_qid, qid)

    def held_tasks(self):
        epic = ops.create_epic(self.conn, 'acme-lib', 'Maintenance')['id']
        source = self.task('Source', epic_id=epic, scope_files=['src/*'])
        dependent = self.task('Dependent', deps=[source])
        transitive = self.task('Transitive', deps=[dependent])
        overlap = self.task('Overlap', epic_id=epic, scope_files=['src/module.py'])
        ops.archive_task(self.conn, overlap)
        ops.add_comment(self.conn, 'task', source, 'owner', 'Pause work', urgent=True)
        ops.replan_hold(self.conn, source)
        return source, dependent, transitive, overlap

    def test_delete_releases_dependents_and_archived_overlaps(self):
        source, *targets = self.held_tasks()
        ops.delete_task(self.conn, source, confirm=True)
        for target in targets:
            task = ops.get_task(self.conn, target)
            self.assertEqual(task['status'], 'ready')
            self.assertIsNone(task['blocker'])
        self.assertEqual(ops.deps_of(self.conn, targets[0]), [])
        self.assertTrue(ops.get_task(self.conn, targets[-1])['archived'])

    def test_delete_keeps_other_sources_and_manual_holds(self):
        source, dependent, _, overlap = self.held_tasks()
        other = self.task('Other source')
        ops.add_hold(self.conn, dependent, other, 'replan')
        ops.block(self.conn, overlap, 'Owner review')
        ops.delete_task(self.conn, source, confirm=True)
        for target, hold in ((dependent, {'source': other, 'reason': 'replan'}),
                             (overlap, {'source': 'manual', 'reason': 'Owner review'})):
            task = ops.get_task(self.conn, target)
            self.assertEqual(task['status'], 'blocked')
            self.assertEqual(task['blocker']['holds'], [hold])

    def test_cleanup_rolls_back_if_deletion_fails(self):
        source, dependent, _, _ = self.held_tasks()
        target = self.task('Gate target')
        qid = self.gate_question(source)
        before = ops.get_task(self.conn, dependent)
        events = ops.events(self.conn)
        settings = [tuple(r) for r in self.conn.execute('SELECT * FROM settings ORDER BY key')]
        self.conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON tasks "
                          "BEGIN SELECT RAISE(ABORT, 'test refusal'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            ops.delete_task(self.conn, source, confirm=True)
        self.assertEqual(ops.get_question(self.conn, qid)['target_id'], source)
        self.assertEqual(ops.get_task(self.conn, dependent), before)
        self.assertEqual(ops.deps_of(self.conn, dependent)[0]['depends_on'], source)
        self.assertEqual(ops.events(self.conn), events)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM settings ORDER BY key')], settings)
        self.assertEqual(ops.get_task(self.conn, target)['status'], 'ready')
