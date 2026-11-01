"""What the scan and the ledger do once git has answered, from a fake repository: recording, refusals, and the owner's answers."""
from unittest.mock import patch
import unittest

from drover import outside_land
from drover.ledger import landing as landing_storage
from drover.ledger import ops, states
from drover.ledger.waiting import Waiting
from tests.outside_land_fixture import Decider, FakeGitCase


class RecordedScans(FakeGitCase):
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


class RecordDetected(FakeGitCase):
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


class LiveCitations(FakeGitCase):
    def test_live_answer_citing_a_commit_on_main_lands(self):
        tid, _, commit = self.unproved(hand='f.txt')
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [tid])
        self.assertEqual(self.status(tid), states.LANDED)
        record = self.external(tid)
        self.assertEqual((record['sha'], record['gate'], record['review']), (commit, 'outside-drover', 'decider'))
        self.assertEqual(ops.events(self.conn, task_id=tid, kind='status')[-1]['actor'], 'detector')
        self.assertEqual(landing_storage.land_sha(self.conn, tid), commit)
        self.assertEqual(ops.open_questions(self.conn), [])
        self.assertEqual(self.conn.execute('SELECT shadow FROM decisions').fetchone()[0], 0)

    def test_live_answer_for_a_commit_off_main_is_ignored(self):
        tid, tip, commit = self.unproved()
        with patch.object(outside_land, 'ambiguous', return_value=[commit, tip]):
            self.scan(Decider(self.conn, acted=True, answer=tip))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_live_citation_with_no_file_overlap_asks_the_owner(self):
        tid, _, commit = self.unproved(hand='g.txt')      # in scope, but the task's recorded commit changed only f.txt
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [])
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertIn(f'Landed at {commit}', self.labels())


class Unknown(FakeGitCase):
    def test_pruned_recorded_commit_still_reaches_the_question(self):
        tid = self.task()
        self.r.branch('feat', {'g.txt': 'y'})
        self.worker_done(tid, 'a' * 40)
        self.r.commit('f.txt', msg='hand work')
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_unresolved_start_still_reaches_the_question(self):
        tid = self.task()
        tip = self.r.branch('feat', {'g.txt': 'y'})
        self.worker_done(tid, tip, base='b' * 40)
        self.r.commit('f.txt', msg='hand work')
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_pruned_later_point_still_reaches_the_question(self):
        tid = self.task()
        tip = self.r.branch('feat', {'g.txt': 'y'})
        self.worker_done(tid, 'c' * 40, run=1)
        self.worker_done(tid, tip, run=2, base='d' * 40)
        self.r.commit('f.txt', msg='hand work')
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(len(ops.open_questions(self.conn)), 1)


class OwnerAnswers(FakeGitCase):
    def refused(self, sha):
        answered = self.question()['id']
        self.answer(f'Landed at {sha}')
        self.scan()
        self.assertEqual(ops.get_question(self.conn, answered)['status'], 'answered')
        return answered

    def test_owner_answer_landed_records_the_named_commit(self):
        tid, _, commit = self.unproved()
        self.scan()
        self.answer(f'Landed at {commit}')
        self.assertEqual(self.scan()['landed'], [tid])
        self.assertEqual(self.status(tid), states.LANDED)
        record = self.external(tid)
        self.assertEqual((record['sha'], record['base_ref'], record['review']), (commit, 'main', 'owner-answer'))
        self.assertEqual(ops.events(self.conn, task_id=tid, kind='status')[-1]['actor'], 'owner')

    def test_owner_answer_off_the_base_opens_one_question_saying_why(self):
        tid, tip, commit = self.unproved()
        with patch.object(outside_land, 'ambiguous', return_value=[commit, tip]):
            self.scan()
        answered = self.refused(tip)
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        again = self.question()
        self.assertNotEqual(again['id'], answered)
        self.assertIn(tip, again['body'])
        self.assertIn('is not on main', again['body'])
        self.assertEqual(self.labels(again), [f'Landed at {commit}', 'Not landed'])
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)


if __name__ == '__main__':
    unittest.main()
