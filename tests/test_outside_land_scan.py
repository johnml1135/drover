"""The scan records what real git proves, and only that."""
import unittest
from unittest.mock import patch

from drover import outside_land, placement
from drover.ledger import landing as landing_storage
from drover.ledger import ops, states
from drover.ledger.waiting import Waiting
from tests.outside_land_fixture import ScanCase


class TickTests(ScanCase):
    def test_yanked_task_lands_and_unblocks_dependents(self):
        tid = self.task('ready')
        follower = ops.create_task(self.conn, 'F', 'acme', scope_files=['g.txt'], deps=[tid])['id']
        ops.approve(self.conn, follower)
        ops.yank(self.conn, tid)
        self.assertNotIn(follower, [t['id'] for t in ops.ready_tasks(self.conn)])
        tip = self.landed_by_hand()
        self.worker_done(tid, tip)
        self.assertEqual(self.scan()['landed'], [tid])
        self.assertEqual(self.status(tid), states.LANDED)
        self.assertIn(follower, [t['id'] for t in ops.ready_tasks(self.conn)])
        self.assertEqual(Waiting(self.conn).pending(('task', follower)), [])
        self.assertEqual(ops.claim_next(self.conn, {})['id'], follower)
        event = ops.events(self.conn, task_id=tid, kind='status')[-1]
        self.assertEqual((event['actor'], event['data']['from'], event['data']['to']),
                         ('detector', states.YANKED, states.LANDED))
        record = event['data']['external_landing']
        self.assertEqual((record['sha'], record['base_ref'], record['gate'], record['review']),
                         (tip, 'main', 'outside-drover', 'ancestor'))
        self.assertEqual(landing_storage.land_sha(self.conn, tid), tip)
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind='task.landed')), 1)

    def test_needs_owner_task_lands(self):
        tid = self.task('ready')
        follower = ops.create_task(self.conn, 'F', 'acme', scope_files=['g.txt'], deps=[tid])['id']
        ops.approve(self.conn, follower)
        ops.set_status(self.conn, tid, states.NEEDS_OWNER, 'worker', reason='stuck')
        self.assertNotIn(follower, [t['id'] for t in ops.ready_tasks(self.conn)])
        self.worker_done(tid, self.landed_by_hand())
        self.assertEqual(self.scan()['landed'], [tid])
        self.assertEqual(self.status(tid), states.LANDED)
        self.assertEqual(Waiting(self.conn).pending(('task', follower)), [])
        self.assertIn(follower, [t['id'] for t in ops.ready_tasks(self.conn)])

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

    def test_second_tick_does_nothing(self):
        tid = self.task()
        self.worker_done(tid, self.landed_by_hand())
        self.scan()
        before = ops.events(self.conn, task_id=tid)
        self.assertEqual(self.scan(), {'scanned': 0, 'landed': []})
        self.assertEqual(ops.events(self.conn, task_id=tid), before)

    def test_a_refused_recording_skips_only_that_task(self):
        first, second = self.task(branch='feat'), self.task(branch='feat')
        tip = self.landed_by_hand()
        self.worker_done(first, tip)
        self.worker_done(second, tip)
        real = ops.record_detected_landing
        def refuse_first(conn, task_id, *args):
            if task_id == first:
                raise states.IllegalTransition('moved on')
            return real(conn, task_id, *args)
        with patch('drover.ledger.ops.record_detected_landing', side_effect=refuse_first):
            self.assertEqual(self.scan()['landed'], [second])

    def test_no_git_inside_a_transaction(self):
        tid = self.task()
        self.worker_done(tid, self.landed_by_hand())
        real = placement.git
        seen = []
        def spy(*args, **kw):
            seen.append((self.conn.in_transaction, kw.get('timeout')))
            return real(*args, **kw)
        with patch.object(placement, 'git', side_effect=spy):
            self.assertEqual(self.scan()['landed'], [tid])
        self.assertTrue(seen)
        self.assertFalse(any(open_tx for open_tx, _ in seen))
        self.assertTrue(all(timeout for _, timeout in seen), seen)


class RecordDetectedLandingTests(ScanCase):
    def record(self, tid, sha, proof='ancestor', base='main'):
        return ops.record_detected_landing(self.conn, tid, sha, base, proof)

    def test_refuses_a_task_that_is_not_yanked_or_needs_owner(self):
        tid = self.task('ready')
        with self.assertRaises(states.IllegalTransition):
            self.record(tid, self.landed_by_hand())

    def test_refuses_a_commit_off_the_base(self):
        tid = self.task()
        tip = self.r.branch('feat', {'f.txt': 'x'})
        with self.assertRaisesRegex(ValueError, 'not verifiably reachable'):
            self.record(tid, tip)
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)

    def test_refuses_a_proof_git_did_not_give(self):
        tid = self.task()
        with self.assertRaisesRegex(ValueError, 'proof'):
            self.record(tid, self.landed_by_hand(), proof='looks landed')

    def test_refuses_the_wrong_base(self):
        tid = self.task()
        with self.assertRaisesRegex(ValueError, 'configured base'):
            self.record(tid, self.landed_by_hand(), base='origin/main')

    def test_repeat_with_the_same_evidence_is_a_no_op(self):
        tid = self.task()
        tip = self.landed_by_hand()
        self.record(tid, tip)
        before = ops.events(self.conn, task_id=tid)
        self.assertEqual(self.record(tid, tip)['status'], states.LANDED)
        self.assertEqual(ops.events(self.conn, task_id=tid), before)

    def test_owner_path_still_refuses_a_yanked_task(self):
        tid = self.task('yanked')
        with self.assertRaises(states.IllegalTransition):
            ops.record_external_landing(self.conn, tid, self.landed_by_hand(), 'main', 'gate', 'review')


if __name__ == '__main__':
    unittest.main()
