"""What the scan looks at and how far it goes, with no repository behind it."""
import unittest
import unittest.mock
from types import SimpleNamespace
from unittest.mock import patch

from drover import dispatcher, git_client, landing, outside_land, placement, repos
from drover.ledger import landing as landing_storage
from drover.ledger import ops, states
from drover.ledger.waiting import Waiting, cause_key
from drover.port import DirectPort
from tests.dispatch_fixture import make_dispatcher
from tests.fakeci import InProcessLauncher, git_answers
from tests.outside_land_fixture import Decider, FakeGitCase, LedgerCase


class CandidateTests(LedgerCase):
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
        self.worker_done(tid, 'b' * 40, run=3, base='c' * 40)
        found = outside_land.candidates(self.conn)[0]
        self.assertEqual((found['commits'], found['start']), (['a' * 40, 'b' * 40], [self.fork, 'a' * 40, 'c' * 40]))

    def test_first_worker_run_with_no_recorded_base_gives_no_start(self):
        tid = self.task()
        self.launched(tid, 1, None)
        self.worker_done(tid, 'b' * 40, run=2)
        self.assertIsNone(outside_land.candidates(self.conn)[0]['start'])

    def test_unconfigured_repo_is_skipped(self):
        tid = ops.create_task(self.conn, 'T', 'elsewhere', branch='feat', scope_files=['f.txt'])['id']
        ops.approve(self.conn, tid)
        ops.set_status(self.conn, tid, states.NEEDS_OWNER, 'worker', reason='stuck')
        self.assertEqual(self.scan()['landed'], [])


class ScanLimitTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.acme, self.beta = str(self.root / 'acme'), str(self.root / 'beta')
        self.programs = {'acme': repos.normalize({'name': 'acme', 'repo': self.acme}),
                         'beta': repos.normalize({'name': 'beta', 'repo': self.beta})}
        loaded = patch('drover.repos.load_all', return_value=self.programs)
        loaded.start()
        self.addCleanup(loaded.stop)

    def repo_task(self, repo='acme'):
        t = ops.create_task(self.conn, 'T', repo, branch='feat', scope_files=['f.txt'])
        ops.approve(self.conn, t['id'])
        ops.set_status(self.conn, t['id'], states.NEEDS_OWNER, 'worker', reason='stuck')
        self.worker_done(t['id'], 'a' * 40)
        return t['id']

    def on_wsl(self, running):
        self.programs['acme'] = repos.normalize({'name': 'acme', 'repo': '/home/owner/acme'})
        return (patch.object(placement, 'wsl_running', return_value=running),
                patch.object(placement, 'routes_through_wsl', side_effect=lambda path, nt=None: str(path).startswith('/')))

    def test_a_timeout_stops_that_repo_and_not_the_others(self):
        self.repo_task('acme'), self.repo_task('acme'), self.repo_task('beta')
        calls = []
        def stuck_acme(repo, *args, **kw):
            calls.append(str(repo))
            if str(repo) == self.acme:
                return git_client.GitReply(git_client.Answer.UNKNOWN, '', '', None, 'timeout')
            return git_client.GitReply(git_client.Answer.NO, '', '', 1, None)
        with patch.object(git_client, 'ask', side_effect=stuck_acme), self.assertLogs('drover.outside_land', 'WARNING'):
            self.assertEqual(self.scan()['scanned'], 2)
        self.assertEqual(calls.count(self.acme), 1)
        self.assertIn(self.beta, calls)

    STDERR = "fatal: unable to access 'https://owner:s3cret@example.test/r.git/': no route\n"

    def failing(self, why, code):
        """`_out` raises GitFailed with this `why` and no stderr in its text; then a scan in which one task fails so.
        tick's one handler for it (section 5.2, outside_land.py:459): a timeout stops the repo, anything else logs."""
        reply = git_client.GitReply(git_client.Answer.UNKNOWN, '', 'fatal: secret-bearing stderr', code, why)
        with patch.object(git_client, 'ask', return_value=reply), self.assertRaises(git_client.GitFailed) as caught:
            outside_land._out(self.acme, 'rev-parse', '--verify', '-q', 'main')
        self.assertEqual((caught.exception.why, caught.exception.code), (why, code))
        self.assertNotIn('secret', str(caught.exception))
        slow, next_in_repo, other = self.repo_task(), self.repo_task(), self.repo_task('beta')
        seen = []
        def detect(conn, task, prog):
            seen.append(task['id'])
            if task['id'] == slow:
                raise git_client.GitFailed(('fetch',), why, code, self.STDERR, '')
            return False
        with patch.object(outside_land, '_detect', side_effect=detect), \
                patch.object(outside_land, '_unproved', return_value=False), \
                self.assertLogs('drover.outside_land', 'WARNING') as logs:
            self.scan()
        return (slow, next_in_repo, other), seen, logs.output

    def test_459_timeout_takes_stuck_and_resume(self):
        """The next task in the stuck repo has the first turn at the next interval."""
        (slow, next_in_repo, other), seen, logs = self.failing('timeout', None)
        self.assertEqual(seen, [slow, other])
        self.assertEqual(ops.get_setting(self.conn, outside_land.RESUME_KEY), next_in_repo)
        self.assertTrue(any('git timed out' in line for line in logs), logs)

    def test_459_any_other_failure_logs_as_464(self):
        for why, code in (('exit', 128), ('unavailable', None)):
            with self.subTest(why=why):
                (slow, next_in_repo, other), seen, logs = self.failing(why, code)
                self.assertEqual(seen[-3:], [slow, next_in_repo, other])     # the repo is not stuck, nothing resumes
                line, = [line for line in logs if f'landing of {slow} not detected' in line]
                self.assertIn("unable to access 'https://example.test/r.git/': no route", line)    # git's words
                self.assertNotIn('s3cret', line)
                self.assertNotIn('owner', line)

    def test_budget_stops_the_scan_and_the_next_one_resumes(self):
        first, second, third = self.repo_task(), self.repo_task(), self.repo_task()
        clock = iter([0, 0, 1, outside_land.SCAN_BUDGET_S])
        seen = []
        def spy(conn, task, prog):
            seen.append(task['id'])
            return False
        with patch.object(outside_land, '_detect', side_effect=spy), patch.object(outside_land, '_unproved', return_value=False), \
                self.assertLogs('drover.outside_land', 'WARNING'):
            self.assertEqual(outside_land.tick(self.conn, {}, now=lambda: next(clock))['scanned'], 2)
        self.assertEqual(seen, [first, second])
        self.assertEqual(ops.get_setting(self.conn, outside_land.RESUME_KEY), third)
        seen.clear()
        with patch.object(outside_land, '_detect', side_effect=spy), patch.object(outside_land, '_unproved', return_value=False):
            self.assertEqual(self.scan()['scanned'], 3)
        self.assertEqual(seen, [third, first, second])
        self.assertEqual(ops.get_setting(self.conn, outside_land.RESUME_KEY), '')

    def test_stopped_wsl_gets_no_git_call(self):
        running, routes = self.on_wsl(False)
        self.repo_task('acme'), self.repo_task('acme')
        spawned = []
        with running as asked, routes, git_answers(lambda repo, *args, timeout: spawned.append(args)):
            self.assertEqual(self.scan(), {'scanned': 0, 'landed': []})
        self.assertEqual(spawned, [])
        self.assertEqual(asked.call_count, 1)

    def test_running_wsl_is_scanned(self):
        running, routes = self.on_wsl(True)
        self.repo_task('acme')
        with running, routes, patch.object(outside_land, '_detect', return_value=False) as detect, \
                patch.object(outside_land, '_unproved', return_value=False):
            self.assertEqual(self.scan()['scanned'], 1)
        detect.assert_called_once()

    def test_task_deleted_during_a_scan_skips_only_that_task(self):
        first, second = self.repo_task(), self.repo_task()
        def gone(conn, task, prog):
            if task['id'] == first:
                raise ops.NotFound(first)
            return False
        with patch.object(outside_land, '_detect', side_effect=gone), patch.object(outside_land, '_unproved', return_value=False), \
                self.assertLogs('drover.outside_land', 'WARNING'):
            self.assertEqual(self.scan()['scanned'], 2)


class ExcludedRepoTests(FakeGitCase):
    def exclude(self, setting='["elsewhere"]'):
        ops.set_setting(self.conn, 'dispatch_repos', setting)

    def test_a_repo_excluded_from_dispatch_gets_no_questions(self):
        self.unproved()
        self.exclude()
        with patch.object(outside_land, '_detect') as detect:
            self.assertEqual(self.scan(), {'scanned': 0, 'landed': []})
        detect.assert_not_called()
        self.assertEqual((self.decider.calls, ops.open_questions(self.conn)), ([], []))

    def test_an_invalid_filter_excludes_every_repo(self):
        self.unproved()
        self.exclude('not json')
        with self.assertLogs('drover.dispatch_repos', 'ERROR'):
            self.assertEqual(self.scan()['scanned'], 0)
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_a_named_repo_is_scanned_in_any_case(self):
        self.unproved()
        self.exclude('["ACME"]')
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)


class QuestionLimitTests(FakeGitCase):
    def backlog(self, count):
        return [self.unproved()[0] for _ in range(count)]

    def asked(self):
        return {q['title'].split()[1] for q in ops.open_questions(self.conn)}

    def test_a_scan_opens_five_questions_and_the_next_ones_follow(self):
        tasks = self.backlog(7)
        self.scan()
        self.assertEqual(self.asked(), set(tasks[:5]))
        self.assertEqual(len(self.decider.calls), 5)            # the sixth stops before its decider call
        self.assertEqual(ops.get_setting(self.conn, outside_land.RESUME_KEY), tasks[5])
        self.scan()
        self.assertEqual(self.asked(), set(tasks))
        self.assertEqual(len(self.decider.calls), 7)
        self.assertEqual(ops.get_setting(self.conn, outside_land.RESUME_KEY), '')

    def test_a_task_the_decider_lands_opens_no_question(self):
        tasks = self.backlog(6)
        decide = Decider(self.conn, acted=True, answer=self.r.base[0])
        with patch.object(outside_land, '_holds', side_effect=lambda repo, task, *a: task['id'] == tasks[0]):
            self.assertEqual(self.scan(decide)['landed'], [tasks[0]])
        self.assertEqual(self.asked(), set(tasks[1:]))

    def test_a_scan_past_its_questions_leaves_an_answer_unused_for_the_next_scan(self):
        answered = self.backlog(1)[0]
        self.scan()
        ops.answer_question(self.conn, self.question()['id'], text=f'Landed at {"f" * 40}')
        fresh = self.backlog(5)
        ops.set_setting(self.conn, outside_land.RESUME_KEY, fresh[0])
        self.scan()
        self.assertEqual(self.asked(), set(fresh))
        self.assertEqual(ops.events(self.conn, task_id=answered, kind='landing.answer'), [])
        self.assertEqual(ops.get_setting(self.conn, outside_land.RESUME_KEY), answered)
        self.scan()
        self.assertEqual([e['data']['outcome'] for e in ops.events(self.conn, task_id=answered, kind='landing.answer')],
                         ['refused'])
        self.assertEqual(self.asked(), {answered, *fresh})

    def test_a_refused_answer_counts_as_a_question_opened(self):
        answered = self.backlog(1)[0]
        self.scan()
        ops.answer_question(self.conn, self.question()['id'], text=f'Landed at {"f" * 40}')
        fresh = self.backlog(5)
        self.scan()
        self.assertEqual(self.asked(), {answered, *fresh[:4]})


class QuestionOptionTests(FakeGitCase):
    def overlapping(self, count):
        tid, _, newest = self.unproved()
        shas = [newest, *(self.r.commit('g.txt', msg=f'edit {n}') for n in range(count - 1))]
        return tid, shas[::-1]

    def test_eight_options_newest_first_saying_how_many_are_left_out(self):
        _, shas = self.overlapping(30)
        self.scan()
        self.assertEqual(self.labels(), [*(f'Landed at {sha}' for sha in shas[:8]), 'Not landed'])
        body = self.question()['body']
        self.assertIn('22 older overlapping commits are left out.', body)
        self.assertIn(shas[7][:12], body)
        self.assertNotIn(shas[8][:12], body)
        self.assertEqual(set(self.decider.calls[0].criteria), {*shas[:20], 'not_landed'})

    def test_nothing_is_said_when_nothing_is_left_out(self):
        self.overlapping(8)
        self.scan()
        self.assertEqual(len(self.labels()), 9)
        self.assertNotIn('left out', self.question()['body'])


class ClosingQuestionTests(FakeGitCase):
    def test_deleting_a_task_closes_its_question_and_leaves_the_rest_working(self):
        gone, kept = self.unproved()[0], self.unproved()[0]
        self.scan()
        ops.delete_task(self.conn, gone, confirm=True)
        ops.yank(self.conn, kept)
        self.scan()
        self.assertEqual(ops.open_questions(self.conn), [])


class ClearHoldTests(LedgerCase):
    def hold(self, repo, sha):
        landing_storage.record_base(self.conn, repo, sha, 'red')
        landing.base_red(self.conn, {}, sha, [], repo)
        return self.conn.execute('SELECT question_id FROM wait_causes WHERE cause=?',
                                 (cause_key('base', repo, sha),)).fetchone()[0]

    def test_clear_hold_is_owner_only_and_answers_retry(self):
        sha = 'a' * 40
        qid = self.hold('acme', sha)
        with self.assertRaises(PermissionError):
            landing.clear_hold(self.conn, 'acme', actor='worker')
        self.assertEqual(ops.get_question(self.conn, qid)['status'], 'open')
        out = landing.clear_hold(self.conn, 'acme')
        self.assertEqual(out, {'cleared': True, 'holds': [{'repo': 'acme', 'sha': sha, 'question': qid}]})
        q = ops.get_question(self.conn, qid)
        self.assertEqual((q['status'], q['answer_choice']), ('answered', landing.RETRY))
        self.assertEqual(Waiting(self.conn).state(cause_key('base', 'acme', sha)), 'authorized')
        self.assertEqual(Waiting(self.conn).pending(('merge_queue', 'acme')), [])
        self.assertIsNone(landing_storage.base_status(self.conn, 'acme', sha))
        self.assertEqual(landing.clear_hold(self.conn, 'acme'), {'cleared': False, 'holds': []})

    def test_clearing_one_repo_leaves_the_others_held(self):
        self.hold('acme', 'a' * 40)
        self.hold('beta', 'b' * 40)
        self.assertEqual({(h['repo'], h['sha']) for h in landing.base_holds(self.conn)},
                         {('acme', 'a' * 40), ('beta', 'b' * 40)})
        self.assertEqual(len(landing.clear_hold(self.conn, 'acme')['holds']), 1)
        self.assertEqual([h['repo'] for h in landing.base_holds(self.conn)], ['beta'])


class DispatcherClockTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.clock = [1_800_000_000.0]

    def dispatcher(self):
        return make_dispatcher(DirectPort(self.conn), launcher=InProcessLauncher(str(self.root / 'runs')), now=lambda: self.clock[0])

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

    def test_config_failure_is_logged(self):
        d = self.clock_only()
        with patch('drover.config.get', side_effect=RuntimeError('no config')), self.assertLogs('drover.dispatcher', 'ERROR'):
            dispatcher.Dispatcher._detect_outside_landings(d)

    def test_a_detected_landing_reaches_open_pages(self):
        d = self.clock_only()
        d.port = SimpleNamespace(get_task=lambda tid: {'id': tid}, _status_changed=unittest.mock.Mock(), conn=self.conn)
        with patch.object(outside_land, 'tick', return_value={'landed': ['T-1']}), patch('drover.config.get', return_value={}):
            dispatcher.Dispatcher._detect_outside_landings(d)
        d.port._status_changed.assert_called_once_with({'id': 'T-1'})

    def test_tick_skips_the_scan_during_a_compaction(self):
        d = self.dispatcher()
        with patch.object(outside_land, 'tick') as scan, patch('drover.compaction.recover', return_value=False), \
                patch('drover.compaction_hold.active', return_value={'phase': 'draining'}), \
                patch.object(dispatcher.Dispatcher, '_compaction_drain', return_value={}):
            d.tick()
        scan.assert_not_called()

    def test_tick_scans_while_paused_and_survives_a_failure(self):
        d = self.dispatcher()
        with patch.object(outside_land, 'tick', side_effect=RuntimeError('boom')) as scan, \
                self.assertLogs('drover.dispatcher', 'ERROR'):
            out = d.tick()
        self.assertTrue(out['paused'])
        self.assertEqual(scan.call_count, 1)


if __name__ == '__main__':
    unittest.main()
