"""The ledger alone decides whether a ready task may resume its gate."""
import tempfile
import unittest
from pathlib import Path

from drover.ledger import connect, ops
from drover.ledger.ops_stages import resume_gate
from tests.ledger_fixture import claim


class GateResume(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.tid = ops.create_task(self.conn, 'task', 'p', scope_files=['src'])['id']
        ops.approve(self.conn, self.tid)

    def worker_done(self):
        claim(self.conn, self.tid) if ops.get_task(self.conn, self.tid)['status'] == 'ready' else None
        run = ops.start_run(self.conn, self.tid, 'worker', 'local')
        ops.finish_run(self.conn, run, 'done')
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.tid,))
        return run

    def test_latest_done_worker_run_resumes(self):
        run = self.worker_done()
        self.assertEqual(resume_gate(self.conn, self.tid, run)['status'], 'running')

    def test_stale_run_id_is_refused(self):
        stale = self.worker_done()
        self.worker_done()
        with self.assertRaisesRegex(ValueError, 'gate resume'):
            resume_gate(self.conn, self.tid, stale)
        self.assertEqual(ops.get_task(self.conn, self.tid)['status'], 'ready')

    def test_already_gated_run_is_refused(self):
        run = self.worker_done()
        gate = ops.start_run(self.conn, self.tid, 'gate', 'local')
        ops.finish_run(self.conn, gate, 'passed')
        ops.amend_run(self.conn, run, outcome='gated')
        with self.assertRaisesRegex(ValueError, 'gate resume'):
            resume_gate(self.conn, self.tid, run)

    def test_passed_gate_after_a_done_run_is_refused_but_failed_gate_is_not(self):
        run = self.worker_done()
        failed = ops.start_run(self.conn, self.tid, 'gate', 'local')
        ops.finish_run(self.conn, failed, 'infrastructure-failed')
        passed = ops.start_run(self.conn, self.tid, 'gate', 'local')
        ops.finish_run(self.conn, passed, 'passed')
        with self.assertRaisesRegex(ValueError, 'gate resume'):
            resume_gate(self.conn, self.tid, run)
        self.conn.execute("UPDATE runs SET outcome='failed' WHERE id=?", (passed,))
        self.assertEqual(resume_gate(self.conn, self.tid, run)['status'], 'running')
