"""Proof-checked stage and output acceptance contracts."""
import tempfile
import unittest
from pathlib import Path

from drover import ci, contract, pipeline
from drover.ledger import connect, ops
from drover.ledger.ops_stages import advance_stage, land_stage, start_running
from tests.pipeline_fixture import MemoryRepo, MemoryRepository

SHA = 'a' * 40


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(self.conn.close)

    def test_writable_approval_requires_scope(self):
        task = ops.create_task(self.conn, 'task', 'p')
        with self.assertRaisesRegex(ValueError, 'scope_files'):
            ops.approve(self.conn, task['id'])
        self.assertEqual(ops.get_task(self.conn, task['id'])['status'], 'proposed')
        ops.update_task(self.conn, task['id'], scope_files=['src'])
        self.assertEqual(ops.approve(self.conn, task['id'])['status'], 'ready')

    def test_generic_stages_refused_and_evidence_advances(self):
        task = ops.create_task(self.conn, 'task', 'p', scope_files=['src'])
        tid = task['id']
        ops.approve(self.conn, tid)
        for actor in ('owner', 'dispatcher'):
            for stage in ('claimed', 'running', 'gated', 'reviewed', 'landed'):
                before = ops.events(self.conn, task_id=tid)
                with self.assertRaises(ValueError):
                    ops.set_status(self.conn, tid, stage, actor)
                self.assertEqual(ops.events(self.conn, task_id=tid), before)
        ops.claim_next(self.conn)
        worker = ops.start_run(self.conn, tid, 'worker', 'codex')
        ops.event(self.conn, 'worker-done', 'dispatcher', {'run': worker, 'sha': SHA}, task_id=tid)
        ops.finish_run(self.conn, worker, 'done')
        gate = ops.start_run(self.conn, tid, 'gate', 'local')
        ops.event(self.conn, 'gate-started', 'dispatcher', {'run': worker, 'gate_run': gate, 'sha': SHA, 'platform': 'windows'}, task_id=tid)
        ops.finish_run(self.conn, gate, 'passed')
        ops.amend_run(self.conn, worker, outcome='gated', gate_receipt=contract.gate_receipt(True, receipt={'sha': SHA, 'status': 'passed'}))
        before = ops.events(self.conn, task_id=tid)
        with self.assertRaisesRegex(ValueError, 'every required platform'):
            advance_stage(self.conn, tid, 'gated', worker, SHA, platforms=['windows', 'wsl'])
        self.assertEqual(ops.events(self.conn, task_id=tid), before)
        self.assertEqual(advance_stage(self.conn, tid, 'gated', worker, SHA, platforms=['windows'])['status'], 'gated')
        review = ops.start_run(self.conn, tid, 'review', 'codex')
        ops.event(self.conn, 'review-started', 'dispatcher', {'run': review, 'worker_run': worker, 'sha': SHA}, task_id=tid)
        ops.finish_run(self.conn, review, 'land', review_verdict='LAND@' + SHA)
        self.assertEqual(advance_stage(self.conn, tid, 'reviewed', review, SHA)['status'], 'reviewed')
        tip = 'b' * 40
        evidence = {'id': tid, 'head': SHA, 'last_sha': tip}
        receipt = {'version': ci.VERSION, 'passed': True, 'sha': tip, 'log': 'receipt'}
        self.assertEqual(land_stage(self.conn, evidence, tip, receipt)['status'], 'landed')
        self.assertEqual(self.conn.execute('SELECT tip FROM task_landed WHERE task_id=?', (tid,)).fetchone()[0], tip)

    def test_running_stage_rejects_a_foreign_or_nonworker_run(self):
        one = ops.create_task(self.conn, 'one', 'p', scope_files=['one'])['id']
        two = ops.create_task(self.conn, 'two', 'p', scope_files=['two'])['id']
        ops.approve(self.conn, one)
        ops.approve(self.conn, two)
        ops.claim_next(self.conn)
        gate = ops.start_run(self.conn, one, 'gate', 'local')
        foreign = ops.start_run(self.conn, two, 'worker', 'local')
        for run in (gate, foreign):
            before = ops.events(self.conn, task_id=one)
            with self.assertRaises(ValueError):
                start_running(self.conn, one, run, 'dispatcher')
            self.assertEqual(ops.events(self.conn, task_id=one), before)
        self.assertEqual(ops.get_task(self.conn, one)['status'], 'claimed')

    def test_existing_output_scope_and_exact_commit(self):
        repo = MemoryRepo(Path(self.tmp.name) / 'git')
        adapter = MemoryRepository(repo)
        base = repo.head()
        task = {'id': 'T-1', 'repo': 'p', 'worktree': str(repo.repo), 'write': True, 'scope_files': ['allowed.txt']}
        pipe = pipeline.Pipeline(None, repos_map={}, repository=adapter)
        bad = repo.commit('other.txt')
        with self.assertRaisesRegex(pipeline.CommitRefused, 'out-of-scope changes: other.txt'):
            pipe.commit_ready(task, 1, '', base=base, sha=bad)
        repo.branches['main'] = base
        from tests.pipeline_fixture import write_tree
        write_tree(repo.repo, repo.tree(base))
        good = repo.commit('allowed.txt')
        self.assertEqual(pipe.commit_ready(task, 1, '', base=base, sha=good), good)
        with self.assertRaisesRegex(pipeline.CommitRefused, 'does not match'):
            pipe.commit_ready(task, 1, '', base=base, sha=bad)
