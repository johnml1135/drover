"""The scan records what real git proves, and only that."""
import unittest
from unittest.mock import patch

from drover import git_client
from drover.ledger import ops, states
from tests.outside_land_fixture import ScanCase


class TickTests(ScanCase):

    def test_branch_at_base_stays_unlanded(self):
        self.r.git('branch', '-q', 'feat', 'main')
        tid = self.task()
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)

    def rerun_on_a_landed_first_run(self):
        """Run 1's commit reaches main by hand, then run 2 starts from a base that holds it and lands too."""
        tid = self.task('yanked')
        first = self.landed_by_hand()
        wt = self.r.root / 'run2-wt'
        self.r.git('worktree', 'add', '-q', wt, 'feat')
        second = self.r.commit('g.txt', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        self.worker_done(tid, first, run=1)
        self.worker_done(tid, second, run=2, base=first)
        self.r.git('merge', '-q', '--ff-only', 'feat')
        return tid, first

    def test_reverting_an_earlier_run_is_not_proved(self):
        tid, first = self.rerun_on_a_landed_first_run()
        self.r.git('revert', '--no-edit', first)
        self.assertFalse((self.r.repo / 'f.txt').exists())
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(self.status(tid), states.YANKED)

    def test_a_rerun_with_both_runs_on_main_lands(self):
        tid, _ = self.rerun_on_a_landed_first_run()
        self.r.commit('later.txt')
        self.assertEqual(self.scan()['landed'], [tid])

    def test_shared_branch_gives_a_task_with_no_commits_nothing(self):
        older = self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'feat')             # an older task's work, landed, its branch left behind
        tid = self.task()
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(self.proof([older])['proof'], 'patch-equivalent')

    def test_landing_reverted_before_the_scan_stays_unlanded(self):
        tid = self.task('yanked')
        self.worker_done(tid, self.landed_by_hand())
        self.r.git('revert', '--no-edit', 'HEAD')
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(self.status(tid), states.YANKED)

    def test_scan_uses_remote_ref_when_push_is_on(self):
        self.use_config(push=True)
        tid = self.task()
        tip = self.landed_by_hand()                   # on local main only: not landed for a pushing repo
        self.worker_done(tid, tip)
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.r.git('push', '-q', 'origin', 'main')
        self.assertEqual(self.scan()['landed'], [tid])
        record = ops.events(self.conn, task_id=tid, kind='status')[-1]['data']['external_landing']
        self.assertEqual(record['base_ref'], 'origin/main')

    def test_local_ref_is_used_when_push_is_off(self):
        tid = self.task()
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('update-ref', 'refs/remotes/origin/main', tip)    # on the tracking ref only
        self.worker_done(tid, tip)
        self.assertEqual(self.scan()['landed'], [])

    def test_no_git_inside_a_transaction(self):
        tid = self.task()
        self.worker_done(tid, self.landed_by_hand())
        real = git_client.ask
        seen = []
        def spy(*args, **kw):
            seen.append((self.conn.in_transaction, kw.get('timeout_s')))
            return real(*args, **kw)
        with patch.object(git_client, 'ask', side_effect=spy):
            self.assertEqual(self.scan()['landed'], [tid])
        self.assertTrue(seen)
        self.assertFalse(any(open_tx for open_tx, _ in seen))
        self.assertTrue(all(timeout for _, timeout in seen), seen)


class RecordDetectedLandingTests(ScanCase):
    def record(self, tid, sha, proof='ancestor', base='main'):
        return ops.record_detected_landing(self.conn, tid, sha, base, proof)

    def test_refuses_the_wrong_base(self):
        tid = self.task()
        with self.assertRaisesRegex(ValueError, 'configured base'):
            self.record(tid, self.landed_by_hand(), base='origin/main')


if __name__ == '__main__':
    unittest.main()
