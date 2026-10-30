"""Existing worker commits use the same scoped acceptance as requested commits."""
from pathlib import Path

from tests.test_pipeline import PolicyBase


class OutputAcceptance(PolicyBase):
    def test_existing_commit_outside_scope_stops_before_gate(self):
        tid = self.task(scope_files=['allowed.txt'])
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid, 'outside.txt'))
        self.d.tick()
        self.assertEqual(self.status(tid), 'needs-owner')
        refused = self.port.events('commit-refused')[-1]['data']
        self.assertEqual(refused['reason'], 'out-of-scope changes: outside.txt')
        self.assertEqual(refused['paths'], ['outside.txt'])
        self.assertEqual(self.fl.of('gate'), [])

    def test_existing_commit_inside_scope_starts_normal_gate(self):
        tid = self.task(scope_files=['allowed.txt'])
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid, 'allowed.txt'))
        self.d.tick()
        self.assertEqual(len(self.fl.of('gate')), 1)
        self.d.tick()
        self.assertEqual(self.status(tid), 'gated')

    def test_run_uses_its_scope_snapshot_even_if_task_scope_changes(self):
        tid = self.task(scope_files=['allowed.txt'])
        self.d.tick()
        self.port.update_task(tid, scope_files=['outside.txt'])
        self.run_worker(tid, self.worker_commits(tid, 'outside.txt'))
        self.d.tick()
        self.assertEqual(self.status(tid), 'needs-owner')
        self.assertEqual(self.port.events('commit-refused')[-1]['data']['paths'], ['outside.txt'])
        self.assertEqual(self.fl.of('gate'), [])

    def test_existing_commit_must_descend_from_run_base_and_checkout_be_clean(self):
        tid = self.task(scope_files=['allowed.txt'])
        self.d.tick()
        sha = self.worker_commits(tid, 'allowed.txt')
        self.fix.commits[sha]['parent'] = None
        self.run_worker(tid, sha)
        self.d.tick()
        self.assertIn('does not descend', self.port.events('commit-refused')[-1]['data']['reason'])
        self.assertEqual(self.fl.of('gate'), [])

    def test_existing_commit_with_dirty_checkout_is_refused(self):
        tid = self.task(scope_files=['allowed.txt', 'extra.txt'])
        self.d.tick()
        sha = self.worker_commits(tid, 'allowed.txt')
        (Path(self.get(tid)['worktree']) / 'extra.txt').write_text('uncommitted')
        self.run_worker(tid, sha)
        self.d.tick()
        self.assertIn('uncommitted changes', self.port.events('commit-refused')[-1]['data']['reason'])
        self.assertEqual(self.fl.of('gate'), [])
