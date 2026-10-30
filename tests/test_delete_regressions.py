"""Task lifecycle preserves shared gate questions and cleans up links and holds atomically."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from drover import pipeline, repos
from drover.ledger.waiting import Waiting, cause_key
from drover.ledger import connect, ops
from drover.port import DirectPort
from hub.store import Store, UiView

class DeleteRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.pipe = pipeline.Pipeline(DirectPort(self.conn), repos_map={
            'acme-lib': {'name': 'acme-lib', 'repo': '/nonexistent/acme-lib'}}, runs_dir=self.tmp.name)

    def task(self, title, **kw):
        kw.setdefault('scope_files', [f"fixture-{ops.get_setting(self.conn, 'seq_T', '0')}"])
        task = ops.create_task(self.conn, title, 'acme-lib', **kw)
        ops.approve(self.conn, task['id'])
        return task['id']

    def gate_question(self, task_id):
        self.assertFalse(self.pipe.prepare_dispatch(ops.get_task(self.conn, task_id)))
        return next(q['id'] for q in Waiting(self.conn).questions() if q['cause'] == cause_key('script', 'acme-lib'))

    def inbox_questions(self):
        store = Store(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(store.close)
        # Exercise the inbox without loading optional task-rendering dependencies.
        view = SimpleNamespace(s=store, c=store.conn, tuner_proposals=lambda: [])
        return UiView.inbox_questions(view)

    def test_archive_keeps_shared_gate_question_actionable_for_remaining_task(self):
        source, target = self.task('Source'), self.task('Target')
        private = ops.ask_question(self.conn, 'task', source, 'Task-specific question')
        qid = self.gate_question(source)
        self.gate_question(target)
        ops.archive_task(self.conn, source)
        self.assertEqual([q['id'] for q in self.inbox_questions()], [str(qid)])
        self.assertEqual(ops.get_question(self.conn, private)['status'], 'open')
        self.assertEqual(Waiting(self.conn).pending(('task', target))[0]['question_id'], qid)
        ops.answer_question(self.conn, qid, text='.drover/scripts/test.ps1')
        self.assertEqual(ops.get_task(self.conn, target)['status'], 'ready')
        self.assertFalse(repos.ci_policy(self.pipe.repo({'repo': 'acme-lib'}))['unknown'])
        self.assertEqual(self.inbox_questions(), [])

    def test_shared_gate_visibility_tracks_remaining_tasks_in_same_repo(self):
        source = self.task('Source')
        qid = self.gate_question(source)
        ops.archive_task(self.conn, source)
        ops.create_task(self.conn, 'Other repo', 'acme-app', scope_files=['fixture-58'])
        self.assertEqual(self.inbox_questions(), [])
        target = self.task('New work')
        self.assertEqual(self.gate_question(target), qid)
        self.assertEqual([q['id'] for q in self.inbox_questions()], [str(qid)])
        ops.archive_task(self.conn, target)
        self.assertEqual(self.inbox_questions(), [])
        ops.archive_task(self.conn, target, False)
        self.assertEqual([q['id'] for q in self.inbox_questions()], [str(qid)])

    def test_delete_removes_related_links_in_both_directions(self):
        before, source, after = sorted(self.task(title) for title in ('Before', 'Source', 'After'))
        ops.update_task(self.conn, source, related=[before, after])
        ops.update_task(self.conn, before, related=[source, after])
        ops.delete_task(self.conn, source, confirm=True)
        self.assertEqual(ops.related_of(self.conn, before), [after])
        self.assertEqual(ops.related_of(self.conn, after), [before])
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(ops.NotFound):
            ops.get_task(self.conn, source)

    def test_open_gate_question_moves_to_surviving_task(self):
        source, target = self.task('Source'), self.task('Target')
        private = ops.ask_question(self.conn, 'task', source, 'Task-specific question')
        qid = self.gate_question(source)
        self.gate_question(target)
        ops.delete_task(self.conn, source, confirm=True)
        self.assertEqual(ops.get_question(self.conn, qid)['target_id'], target)
        with self.assertRaises(ops.NotFound):
            ops.get_question(self.conn, private)
        ops.answer_question(self.conn, qid, text='.drover/scripts/test.ps1')
        self.assertFalse(repos.ci_policy(self.pipe.repo({'repo': 'acme-lib'}))['unknown'])
        self.assertEqual(ops.get_task(self.conn, target)['status'], 'ready')

    def test_answered_gate_keeps_owner_provenance_and_command(self):
        source, target = self.task('Source'), self.task('Target')
        qid = self.gate_question(source)
        ops.answer_question(self.conn, qid, text='.drover/scripts/test.ps1')
        command = Waiting(self.conn).answer(cause_key('script', 'acme-lib'))
        ops.archive_task(self.conn, target)
        ops.delete_task(self.conn, source, confirm=True)
        self.assertEqual(ops.get_question(self.conn, qid)['target_id'], target)
        self.assertEqual(Waiting(self.conn).answer(cause_key('script', 'acme-lib')), command)
        self.assertFalse(repos.ci_policy(self.pipe.repo({'repo': 'acme-lib'}))['unknown'])

    def test_last_task_clears_gate_references_before_next_task(self):
        source = self.task('Source')
        qid = self.gate_question(source)
        ops.answer_question(self.conn, qid, text='.drover/scripts/test.ps1')
        ops.delete_task(self.conn, source, confirm=True)
        self.assertIsNone(Waiting(self.conn).answer(cause_key('script', 'acme-lib')))
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
        ops.add_comment(self.conn, 'task', source, 'owner', 'Revise plan', replan=True)
        return source, dependent, transitive, overlap

    def test_delete_releases_dependents_and_archived_overlaps(self):
        source, *targets = self.held_tasks()
        ops.delete_task(self.conn, source, confirm=True)
        for target in targets:
            task = ops.get_task(self.conn, target)
            self.assertEqual(task['status'], 'ready')
            self.assertEqual(Waiting(self.conn).pending(('task', target)), [])
        self.assertEqual(ops.deps_of(self.conn, targets[0]), [])
        self.assertTrue(ops.get_task(self.conn, targets[-1])['archived'])

    def test_delete_keeps_other_sources_and_manual_holds(self):
        source, dependent, _, overlap = self.held_tasks()
        other = self.task('Other source')
        ops.add_dep(self.conn, dependent, other)
        ops.add_comment(self.conn, 'task', other, 'owner', 'Revise other plan', replan=True)
        ops.block(self.conn, overlap, 'Owner review')
        ops.delete_task(self.conn, source, confirm=True)
        from drover.ledger.waiting import Waiting, cause_key
        waiting = Waiting(self.conn)
        for target in (dependent, overlap):
            self.assertEqual(ops.get_task(self.conn, target)['status'], 'blocked')
            rows = waiting.pending(("task", target))
            self.assertEqual(len(rows), 1)
            if target == dependent:
                self.assertEqual(rows[0]['data']['source'], other)
            else:
                self.assertEqual(rows[0]['cause'], cause_key("manual", overlap))
                self.assertEqual(rows[0]['data']['note'], 'Owner review')

    def test_cleanup_rolls_back_if_deletion_fails(self):
        source, dependent, _, _ = self.held_tasks()
        target = self.task('Gate target')
        ops.update_task(self.conn, source, related=[target])
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
        self.assertEqual(ops.related_of(self.conn, source), [target])
        self.assertEqual(ops.events(self.conn), events)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM settings ORDER BY key')], settings)
        self.assertEqual(ops.get_task(self.conn, target)['status'], 'ready')
