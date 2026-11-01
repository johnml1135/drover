"""Git proves a landing made outside Drover, and nothing weaker does."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from drover import ci, dispatcher, landing, outside_land, repos
from drover.ledger import connect, ops, states
from drover.ledger import landing as landing_storage
from drover.ledger.waiting import Waiting, cause_key
from drover.port import DirectPort
from tests.dispatch_fixture import make_dispatcher
from tests.fakeci import InProcessLauncher
from tests.gitfix import Repo


class GitCase(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)
        self.r.git('checkout', '-q', 'main')

    def proof(self, commits=(), branch='feat', base='main'):
        return outside_land.git_proof(self.r.repo, branch, base, commits)

    def main(self):
        return self.r.git('rev-parse', 'main')


class GitProofTests(GitCase):
    def test_fast_forward_is_ancestor(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertEqual(self.proof([tip]), {'sha': tip, 'proof': 'ancestor'})

    def test_rebased_is_patch_equivalent(self):
        self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'feat')
        self.assertEqual(self.proof(), {'sha': self.main(), 'proof': 'patch-equivalent'})

    def test_branch_rebased_then_fast_forwarded_is_patch_equivalent(self):
        recorded = self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        wt = self.r.root / 'rebase-wt'
        self.r.git('worktree', 'add', '-q', wt, 'feat')
        self.r.git('rebase', '-q', 'main', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertEqual(self.proof([recorded]), {'sha': self.main(), 'proof': 'patch-equivalent'})
        self.assertIsNone(self.proof())

    def test_squash_merge_adds_nothing(self):
        tip = self.r.branch('feat', {'a.txt': 'a', 'b.txt': 'b'})
        self.r.git('merge', '-q', '--squash', 'feat')
        self.r.git('commit', '-q', '-m', 'squash')
        self.assertEqual(self.proof([tip]), {'sha': self.main(), 'proof': 'adds-nothing'})

    def test_adds_nothing_alone_is_no_proof_without_recorded_commits(self):
        self.r.branch('feat', {'a.txt': 'a', 'b.txt': 'b'})
        self.r.git('merge', '-q', '--squash', 'feat')
        self.r.git('commit', '-q', '-m', 'squash')
        self.assertIsNone(self.proof())

    def test_partial_squash_is_not_proved(self):
        tip = self.r.branch('feat', {'a.txt': 'a', 'b.txt': 'b'})
        self.r.git('checkout', '-q', 'feat', '--', 'a.txt')
        self.r.git('commit', '-q', '-m', 'part of the branch')
        self.assertIsNone(self.proof([tip]))

    def test_unlanded_branch_is_not_proved(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        self.assertIsNone(self.proof([tip]))

    def test_branch_at_base_is_not_a_landing(self):
        self.r.git('branch', '-q', 'feat', 'main')
        self.assertIsNone(self.proof())
        self.r.commit('later.txt')                  # now behind the base as well
        self.assertIsNone(self.proof())

    def test_branch_with_no_own_commits_after_rebase_needs_task_evidence(self):
        unlanded = self.r.branch('feat', {'f.txt': 'x'})
        other = self.r.commit('other.txt')
        self.r.git('branch', '-q', '-f', 'feat', 'main')   # rebased: every task commit dropped
        self.assertIsNone(self.proof())
        self.assertIsNone(self.proof([unlanded]))          # a recorded commit that never reached the base
        self.assertEqual(self.proof([unlanded, other]), {'sha': other, 'proof': 'ancestor'})

    def test_earlier_attempt_on_the_base_does_not_land_the_later_one(self):
        first = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        wt = self.r.root / 'retry-wt'
        self.r.git('worktree', 'add', '-q', wt, 'feat')
        second = self.r.commit('g.txt', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        self.r.git('branch', '-q', '-f', 'feat', 'main')
        self.assertIsNone(self.proof([first, second]))

    def test_own_commits_block_the_ancestor_shortcut(self):
        landed = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.git('branch', '-q', 'wt', 'feat')
        self.r.git('worktree', 'add', '-q', self.r.root / 'wt', 'wt')
        self.r.commit('more.txt', cwd=self.r.root / 'wt')
        self.assertIsNone(self.proof([landed], branch='wt'))

    def test_branch_that_undoes_itself_is_not_proved(self):
        self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('checkout', '-q', 'feat')
        self.r.git('revert', '--no-edit', 'HEAD')
        tip = self.r.git('rev-parse', 'HEAD')
        self.r.git('checkout', '-q', 'main')
        self.r.commit('other.txt')
        self.assertIsNone(self.proof([tip]))

    def test_merge_commit_hides_nothing_from_the_patch_check(self):
        self.r.branch('feat', {'f.txt': 'x'})
        self.r.branch('side', {'s.txt': 's'})
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'feat', 'side')
        wt = self.r.root / 'merge-wt'
        self.r.git('worktree', 'add', '-q', wt, 'feat')
        self.r.git('merge', '-q', '--no-edit', 'side', cwd=wt)
        (wt / 'unlanded.txt').write_text('in the merge only')
        self.r.git('add', 'unlanded.txt', cwd=wt)
        self.r.git('commit', '-q', '--amend', '--no-edit', cwd=wt)
        tip = self.r.git('rev-parse', 'HEAD', cwd=wt)
        self.assertTrue(landing.contained(self.r.repo, tip, self.main()))    # cherry skips the merge
        self.assertIsNone(self.proof([tip]))

    def test_branch_with_only_a_merge_of_the_base_has_no_commits_of_its_own(self):
        self.r.git('branch', '-q', 'feat', 'main')
        self.r.commit('other.txt')
        self.r.git('checkout', '-q', 'feat')
        self.r.git('merge', '-q', '--no-ff', '--no-edit', 'main')
        self.r.git('checkout', '-q', 'main')
        self.r.commit('later.txt')
        self.assertIsNone(self.proof([]))

    def test_missing_branch_is_unknown(self):
        self.assertIsNone(self.proof(branch='no-such-branch'))

    def test_missing_base_is_unknown(self):
        self.r.branch('feat', {'f.txt': 'x'})
        self.assertIsNone(self.proof(base='no-such-base'))

    def test_git_failure_is_unknown(self):
        self.assertIsNone(outside_land.git_proof(self.r.root / 'not-a-repo', 'feat', 'main', ['a' * 40]))
        self.r.branch('feat', {'f.txt': 'x'})
        for error in (OSError('no git'), ci.CIError('broken')):
            with patch.object(landing, 'git', side_effect=error):
                self.assertIsNone(self.proof())


class ScanCase(GitCase):
    def setUp(self):
        super().setUp()
        self.conn = connect(self.r.root / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.use_config()

    def use_config(self, push=False):
        prog = repos.normalize({'name': 'acme', 'repo': str(self.r.repo), 'landing': {'push': push}})
        loaded = patch('drover.repos.load_all', return_value={'acme': prog})
        loaded.start()
        self.addCleanup(loaded.stop)

    def task(self, status='needs-owner', branch='feat', **kw):
        kw.setdefault('scope_files', ['f.txt'])
        t = ops.create_task(self.conn, 'T', 'acme', branch=branch, **kw)
        ops.approve(self.conn, t['id'])
        if status == 'needs-owner':
            ops.set_status(self.conn, t['id'], states.NEEDS_OWNER, 'worker', reason='stuck')
        elif status == 'yanked':
            ops.yank(self.conn, t['id'])
        return t['id']

    def worker_done(self, tid, sha):
        ops.event(self.conn, 'worker-done', 'dispatcher', {'run': 1, 'sha': sha}, task_id=tid)

    def scan(self):
        return outside_land.tick(self.conn, {})

    def landed_by_hand(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        return tip

    def status(self, tid):
        return ops.get_task(self.conn, tid)['status']


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

    def test_running_task_is_not_scanned(self):
        tid = self.task('ready')
        self.assertEqual(ops.claim_next(self.conn, {})['id'], tid)
        ops.start_run(self.conn, tid, 'worker', 'claude')
        self.assertEqual(self.status(tid), states.RUNNING)
        self.assertEqual(outside_land.candidates(self.conn), [])

    def test_yanked_task_with_a_live_run_is_not_scanned(self):
        tid = self.task('ready')
        ops.claim_next(self.conn, {})
        ops.start_run(self.conn, tid, 'worker', 'claude')
        ops.yank(self.conn, tid)
        self.assertEqual(outside_land.candidates(self.conn), [])

    def test_candidates_are_write_tasks_with_a_branch(self):
        wanted = self.task('yanked')
        self.task('needs-owner', branch=None)
        self.task('needs-owner', write=False, scope_files=[])
        self.assertEqual([t['id'] for t in outside_land.candidates(self.conn)], [wanted])

    def test_candidates_carry_the_recorded_commits(self):
        tid = self.task()
        self.worker_done(tid, 'a' * 40)
        ops.event(self.conn, 'worker-done', 'dispatcher', {'run': 2, 'sha': ''}, task_id=tid)
        self.worker_done(tid, 'b' * 40)
        self.assertEqual(outside_land.candidates(self.conn)[0]['commits'], ['a' * 40, 'b' * 40])

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

    def test_unconfigured_repo_is_skipped(self):
        tid = ops.create_task(self.conn, 'T', 'elsewhere', branch='feat', scope_files=['f.txt'])['id']
        ops.approve(self.conn, tid)
        ops.set_status(self.conn, tid, states.NEEDS_OWNER, 'worker', reason='stuck')
        self.assertEqual(self.scan()['landed'], [])

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
        from drover import placement
        real = placement.git
        seen = []
        def spy(*args, **kw):
            seen.append(self.conn.in_transaction)
            return real(*args, **kw)
        with patch.object(placement, 'git', side_effect=spy):
            self.assertEqual(self.scan()['landed'], [tid])
        self.assertTrue(seen)
        self.assertFalse(any(seen))


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


class ClearHoldTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)
        self.conn = connect(self.r.root / 'ledger.db')
        self.addCleanup(self.conn.close)

    def hold(self, repo, sha):
        landing_storage.record_base(self.conn, repo, sha, 'red')
        landing.base_red(self.conn, {}, sha, [], repo)
        return self.conn.execute('SELECT question_id FROM wait_causes WHERE cause=?',
                                 (cause_key('base', repo, sha),)).fetchone()[0]

    def test_clear_hold_is_owner_only_and_answers_retry(self):
        sha = 'a' * 40
        qid = self.hold('acme', sha)
        with self.assertRaises(PermissionError):
            landing.clear_hold(self.conn, actor='worker')
        self.assertEqual(ops.get_question(self.conn, qid)['status'], 'open')
        out = landing.clear_hold(self.conn)
        self.assertEqual(out, {'cleared': True, 'holds': [{'repo': 'acme', 'sha': sha, 'question': qid}]})
        q = ops.get_question(self.conn, qid)
        self.assertEqual((q['status'], q['answer_choice']), ('answered', landing.RETRY))
        self.assertEqual(Waiting(self.conn).state(cause_key('base', 'acme', sha)), 'authorized')
        self.assertEqual(Waiting(self.conn).pending(('merge_queue', 'acme')), [])
        self.assertIsNone(landing_storage.base_status(self.conn, 'acme', sha))
        self.assertEqual(landing.clear_hold(self.conn), {'cleared': False, 'holds': []})

    def test_every_held_repo_is_listed_and_cleared(self):
        self.hold('acme', 'a' * 40)
        self.hold('beta', 'b' * 40)
        self.assertEqual({(h['repo'], h['sha']) for h in landing.base_holds(self.conn)},
                         {('acme', 'a' * 40), ('beta', 'b' * 40)})
        self.assertEqual(len(landing.clear_hold(self.conn)['holds']), 2)
        self.assertEqual(landing.base_holds(self.conn), [])


class DispatcherClockTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)
        self.conn = connect(self.r.root / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.clock = [1_800_000_000.0]

    def dispatcher(self):
        return make_dispatcher(DirectPort(self.conn), launcher=InProcessLauncher(str(self.r.root / 'runs')), now=lambda: self.clock[0])

    def clock_only(self):
        return SimpleNamespace(now=lambda: self.clock[0], _outside_at=None, port=SimpleNamespace(conn=self.conn))

    def test_detection_runs_once_per_interval(self):
        d = self.clock_only()
        with patch.object(outside_land, 'tick') as scan, patch('drover.config.get', return_value={}):
            dispatcher.Dispatcher._detect_outside_landings(d)
            self.clock[0] += 599
            dispatcher.Dispatcher._detect_outside_landings(d)
            self.assertEqual(scan.call_count, 1)
            self.clock[0] += 1
            dispatcher.Dispatcher._detect_outside_landings(d)
            self.assertEqual(scan.call_count, 2)

    def test_watch_config_sets_the_interval(self):
        d = self.clock_only()
        with patch.object(outside_land, 'tick') as scan, \
                patch('drover.config.get', return_value={'watch': {'interval_s': 60}}):
            dispatcher.Dispatcher._detect_outside_landings(d)
            self.clock[0] += 60
            dispatcher.Dispatcher._detect_outside_landings(d)
        self.assertEqual(scan.call_count, 2)

    def test_detection_failure_is_logged_and_spends_the_interval(self):
        d = self.clock_only()
        with patch.object(outside_land, 'tick', side_effect=RuntimeError('boom')) as scan, \
                patch('drover.config.get', return_value={}), self.assertLogs('drover.dispatcher', 'ERROR'):
            dispatcher.Dispatcher._detect_outside_landings(d)
            dispatcher.Dispatcher._detect_outside_landings(d)
        self.assertEqual(scan.call_count, 1)

    def test_tick_scans_while_paused_and_survives_a_failure(self):
        d = self.dispatcher()
        with patch.object(outside_land, 'tick', side_effect=RuntimeError('boom')) as scan, \
                self.assertLogs('drover.dispatcher', 'ERROR'):
            out = d.tick()
        self.assertTrue(out['paused'])
        self.assertEqual(scan.call_count, 1)


if __name__ == '__main__':
    unittest.main()
