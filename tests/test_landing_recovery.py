"""Landing recovery policy, with in-memory ledger and no external processes."""
import json
import sqlite3
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from drover.port import DirectPort
from drover import ci, landing, landing_control, origin, resources
from drover.ledger import db, ops
from drover.ledger.waiting import Waiting, BASE, cause_key
from tests.fakeci import git_answers, resources_go
from tests.test_origin_sync import FakeGit, NEW, OLD, REPO, listing, world

SHA = 'a' * 40
INFRA = dict(status='failed', outcome='infrastructure-failed', passed=False, sha='b' * 40,
             platform='wsl', run_id='ci-timeout', reason='timeout', log_tail=['CI timed out (wall)'])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        task = ops.create_task(self.conn, 'repair', 'p')
        self.task = dict(id=task['id'], head='c' * 40, branch='topic', title='repair', files=['x'])
        self.stack = self.enterContext(ExitStack())
        for obj, name, value in (
            (landing, 'sync_main', None), (landing, '_control_guard', 0),
            (landing, 'form_batch', [self.task]),
            (landing, 'build_tip', ('b' * 40, [self.task], [])), (landing_control, 'build_tip', ('b' * 40, [self.task], [])),
        ):
            self.stack.enter_context(patch.object(obj, name, return_value=value))
        self.git_calls = []
        self.stack.enter_context(git_answers(lambda repo, *args, timeout: self.git_calls.append(args)
                                             or subprocess.CompletedProcess(args, 0, SHA, '')))
        self.stack.enter_context(patch('drover.ownership.owned_by', return_value=False))
        self.stack.enter_context(patch.object(resources, 'check', side_effect=resources_go))  # landing's own retry probe
        self.clock = self.stack.enter_context(patch.object(landing.time, 'time', return_value=1000))

    def run_batch(self, gate):
        return landing.run_batch(self.conn, '.', gate, land_stage=DirectPort(self.conn).land_stage, workdir=Path('unused-recovery-work'), repo_name='p',
                                 required_platforms=['windows', 'wsl'])

    def base_retry(self):
        waiting = Waiting(self.conn)
        cause = cause_key('base', 'p', SHA)
        waiting.wait(('merge_queue', 'p'), BASE, cause_key('p', SHA), cause=cause,
                     data=dict(repo='p', sha=SHA, mode='blocked'))
        q = waiting.ask(cause, 'task', self.task['id'], 'base red', 'retry',
                        options=[dict(label=landing.RETRY)], allow_text=False)
        ops.answer_question(self.conn, q, choice=landing.RETRY)
        return waiting, cause

    def test_infra_retries_once_then_holds_with_one_question(self):
        gate = Mock(return_value=INFRA)
        self.assertEqual(self.run_batch(gate)['outcome'], 'infra-retry')
        self.assertEqual(self.run_batch(gate)['outcome'], 'queued')
        self.clock.return_value = 1100
        self.assertEqual(self.run_batch(gate)['outcome'], 'infra-held')
        self.assertEqual(self.run_batch(gate)['outcome'], 'infra-held')
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(len(ops.events(self.conn, kind='question-asked')), 1)

    def recovery_keys(self):
        return [r[0] for r in self.conn.execute("SELECT key FROM settings WHERE key LIKE 'landing_recovery:%'")]

    def test_owner_acknowledgement_gives_held_task_one_fresh_attempt(self):
        gate = Mock(return_value=INFRA)
        self.run_batch(gate)
        self.clock.return_value = 1100
        self.assertEqual(self.run_batch(gate)['outcome'], 'infra-held')
        self.assertEqual(self.run_batch(gate)['outcome'], 'infra-held')
        self.assertEqual(gate.call_count, 2)
        question = ops.events(self.conn, kind='question-asked')[-1]['data']['question']
        ops.answer_question(self.conn, question, choice='Retry on a healthy host')
        self.assertEqual(self.recovery_keys() and len(self.recovery_keys()), 1)
        self.assertEqual(self.run_batch(gate)['outcome'], 'infra-retry')
        self.assertEqual(gate.call_count, 3)

    def test_landing_clears_recovery_state(self):
        self.run_batch(Mock(return_value=INFRA))
        self.assertEqual(len(self.recovery_keys()), 1)
        self.clock.return_value = 1100
        with patch.object(landing, '_drive', side_effect=lambda *a, **k: a[5].update(outcome='landed', landed=[self.task['id']])):
            self.assertEqual(self.run_batch(Mock())['outcome'], 'landed')
        self.assertEqual(self.recovery_keys(), [])

    def test_retry_requires_healthy_resources(self):
        gate = Mock(return_value=INFRA)
        self.run_batch(gate)
        self.clock.return_value = 1100
        with patch.object(resources, 'check', return_value=dict(verdict='queue', reasons=['host busy'])):
            self.assertEqual(self.run_batch(gate)['outcome'], 'queued')
        self.assertEqual(gate.call_count, 1)

    def test_consumed_owner_retry_finishes_on_infrastructure_failure(self):
        waiting, cause = self.base_retry()
        self.assertEqual(self.run_batch(Mock(return_value=INFRA))['outcome'], 'infra-retry')
        self.assertNotEqual(waiting.state(cause), 'attempting')

    def test_gate_preserves_infrastructure_classification(self):
        result = dict(gates=0, red=0, bisect_gates=0, gate_s=[])
        with self.assertRaises(ci.CIInfrastructure) as caught:
            landing._gate(Mock(return_value=INFRA), '.', 'b' * 40, [], result)
        self.assertEqual(caught.exception.detail['platform'], 'wsl')

    def test_ci_run_returns_typed_timeout_instead_of_throwing(self):
        launcher = SimpleNamespace(launch=Mock(return_value=SimpleNamespace(
            poll=lambda: dict(status='timeout', returncode=None, timeout='wall'))))
        service = ci.CI('unused-ci-recovery', launcher, resource_check=resources_go)
        spec = dict(run_id='ci-timeout', nonce='nonce', placement='wsl')
        with patch.object(ci.repos, 'load', side_effect=lambda prog, *a: prog), \
                patch.object(service, 'prepare', return_value=dict(spec=spec)), \
                patch.object(service, 'result', return_value=dict(receipt=None, outcome='infrastructure-failed', reason='timeout', nonce='nonce', run_id=spec['run_id'], excerpt='wall timeout')), \
                patch('drover.disk.read', return_value=[]), \
                patch.object(ci, 'durable'):
            result = service.run({}, '.', SHA, platforms=['wsl'])
        self.assertEqual((result['status'], result['reason'], result['run_id']),
                         ('failed', 'timeout', 'ci-timeout'))

    def test_budget_survives_batch_membership_and_replayed_tip_changes(self):
        gate = Mock(return_value=INFRA)
        self.run_batch(gate)
        self.clock.return_value = 1100
        other = dict(self.task, id=ops.create_task(self.conn, 'other', 'p')['id'])
        with patch.object(landing, 'form_batch', return_value=[self.task, other]), \
                patch.object(landing, 'build_tip', return_value=('d' * 40, [self.task, other], [])):
            self.assertEqual(self.run_batch(gate)['outcome'], 'infra-held')
        self.assertEqual(gate.call_count, 2)

    def test_failure_detail_and_task_event_are_atomic(self):
        result = self.run_batch(Mock(return_value=INFRA))
        row = self.conn.execute('SELECT * FROM landing_batches').fetchone()
        detail = json.loads(row['detail'])
        self.assertEqual((detail['reason'], detail['run_id'], detail['platform']),
                         ('timeout', 'ci-timeout', 'wsl'))
        self.assertIn('timed out', detail['log_excerpt'])
        event, = ops.events(self.conn, kind='landing.outcome')
        self.assertEqual(event['task_id'], self.task['id'])
        self.assertEqual(event['data']['batch_id'], result['batch_id'])
        self.assertEqual(event['data']['detail'], detail)
        from drover.ledger import landing as storage
        with patch.object(ops, 'event', side_effect=RuntimeError('notice unavailable')):
            with self.assertRaises(RuntimeError):
                storage.record_batch(self.conn, result)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM landing_batches').fetchone()[0], 1)

    def test_non_landed_rows_record_cause(self):
        tid = self.task['id']
        def drive(outcome, **extra):
            return lambda *a, **k: a[5].update(outcome=outcome, **extra)
        def detail():
            return json.loads(self.conn.execute('SELECT detail FROM landing_batches ORDER BY id DESC').fetchone()['detail'])
        with patch.object(landing, '_drive', side_effect=drive('conflict', dropped={tid: 'rebase conflict'})):
            self.run_batch(Mock())
        self.assertIn(tid, detail()['reason'])
        self.assertIn('rebase conflict', detail()['reason'])
        with patch.object(landing, '_drive', side_effect=drive('base-red', base_sha=SHA, detail={'run_id': 'r1'})):
            self.run_batch(Mock())
        self.assertIn(SHA, detail()['reason'])
        self.assertEqual((detail()['sha'], detail()['run_id']), (SHA, 'r1'))
        with patch.object(landing, '_drive', side_effect=ValueError('bad thing')), self.assertRaises(ValueError):
            self.run_batch(Mock())
        self.assertIn('bad thing', detail()['error'])
        self.assertIn('bad thing', detail()['reason'])

    def test_gate_exception_records_timing_and_reason(self):
        gate = Mock(side_effect=RuntimeError('broken adapter'))
        with self.assertRaisesRegex(RuntimeError, 'broken adapter'):
            self.run_batch(gate)
        row = self.conn.execute('SELECT * FROM landing_batches').fetchone()
        self.assertEqual((row['gates'], row['red']), (1, 1))
        self.assertEqual(len(json.loads(row['gate_s'])), 1)
        self.assertIn('broken adapter', json.loads(row['detail'])['reason'])

    def test_gate_setting_exposes_detail_and_exhaustion_is_issue(self):
        from hub.dispatch import LandingService
        from drover.notifications.core import classify
        self.run_batch(Mock(return_value=INFRA))
        self.clock.return_value = 1100
        self.run_batch(Mock(return_value=INFRA))
        LandingService._write_gate(self.conn)
        setting = json.loads(ops.get_setting(self.conn, 'gate'))
        self.assertEqual(setting['status'], 'infra-held')
        self.assertEqual(setting['detail']['run_id'], 'ci-timeout')
        event = ops.events(self.conn, kind='landing.outcome')[-1]
        self.assertEqual(classify(self.conn, event), 'issue')

    def test_load_queues_before_launch_and_releases_runner_ticket(self):
        from drover.ledger import ci as slots
        launcher = SimpleNamespace(launch=Mock())
        service = ci.CI('unused-ci-recovery', launcher,
                        resource_check=Mock(return_value=dict(verdict='queue', reasons=['load too high'])))
        spec = dict(run_id='ci-queued', nonce='nonce', placement='wsl')
        with patch.object(ci.repos, 'load', side_effect=lambda prog, *a: prog), \
                patch.object(service, 'prepare', return_value=dict(spec=spec)), \
                patch('drover.disk.read', return_value=[]), \
                patch.object(ci, 'durable') as save:
            with self.assertRaisesRegex(ci.CIDeferred, 'load too high'):
                service.run({}, '.', SHA, platforms=['wsl'], conn=self.conn)
        launcher.launch.assert_not_called()
        self.assertEqual(slots.lease_queue(self.conn), [])
        checkpoint = save.write_json.call_args.args[1]
        self.assertEqual(checkpoint['execution'], 'prepared')
        self.assertNotIn('ticket', checkpoint)

    def test_resource_deferral_preserves_work_without_spending_recovery(self):
        result = self.run_batch(Mock(side_effect=ci.CIDeferred('host busy')))
        self.assertEqual(result['outcome'], 'queued')
        self.assertEqual((result['gates'], result['gate_s'], result['red']), (0, [], 0))
        self.assertFalse(any(args[:2] == ('worktree', 'remove') for args in self.git_calls))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'landing_recovery:%'").fetchone()[0], 0)

    def designate_repair(self):
        landing.designate_base_repair(self.conn, 'p', SHA, self.task['id'], self.task['head'])

    def hold_base(self):
        waiting = Waiting(self.conn)
        cause = cause_key('base', 'p', SHA)
        waiting.wait(('merge_queue', 'p'), BASE, cause_key('p', SHA), cause=cause,
                     data=dict(repo='p', sha=SHA, mode='blocked'))
        return waiting, cause

    def test_designated_repair_alone_passes_base_hold_and_runs_fresh_gate(self):
        self.hold_base()
        self.designate_repair()
        receipt = dict(version=1, status='passed', outcome='passed', passed=True, exit_code=0, tree_unchanged=True, sha='b' * 40, kind='full', platform='windows+wsl', results=[
            dict(version=1, status='passed', outcome='passed', passed=True, exit_code=0, tree_unchanged=True, sha='b' * 40, platform=p, kind='full') for p in ('windows', 'wsl')])
        gate = Mock(return_value=receipt)
        with patch.object(landing, 'build_tip', side_effect=lambda repo, work, base, tasks: ('b' * 40, tasks, [])), \
                patch.object(landing, '_land', side_effect=lambda *args, **k: args[6].update(outcome='landed')):
            result = self.run_batch(gate)
        self.assertEqual(result['outcome'], 'landed')
        self.assertEqual([t['id'] for t in gate.call_args.args[2]], [self.task['id']])
        self.assertTrue(gate.call_args.args[2][0]['base_repair'])

    def test_undesignated_task_stays_held(self):
        self.hold_base()
        gate = Mock()
        self.assertEqual(self.run_batch(gate)['outcome'], 'held')
        gate.assert_not_called()

    def test_blocked_repair_cycle_notifies_once_and_does_not_regate(self):
        self.hold_base()
        self.designate_repair()
        gate = Mock(return_value=dict(status='failed', passed=False, platform='wsl'))
        with patch.object(landing, 'base_status', return_value='red'):
            self.assertEqual(self.run_batch(gate)['outcome'], 'repair-blocked')
            self.assertEqual(self.run_batch(gate)['outcome'], 'repair-blocked')
        self.assertEqual(gate.call_count, 1)
        notices = ops.events(self.conn, kind='landing.repair-blocked')
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['task_id'], self.task['id'])

    def test_stale_repair_head_cannot_bypass_hold(self):
        self.hold_base()
        self.designate_repair()
        with patch.object(landing, 'form_batch', return_value=[dict(self.task, head='d' * 40)]):
            gate = Mock()
            self.assertEqual(self.run_batch(gate)['outcome'], 'repair-blocked')
            gate.assert_not_called()

    def test_repair_infrastructure_uses_same_bounded_budget(self):
        self.hold_base()
        self.designate_repair()
        gate = Mock(return_value=INFRA)
        self.assertEqual(self.run_batch(gate)['outcome'], 'infra-retry')
        self.clock.return_value = 1100
        self.assertEqual(self.run_batch(gate)['outcome'], 'infra-held')
        self.assertEqual(gate.call_count, 2)

    def test_repair_windows_only_receipt_cannot_land(self):
        self.hold_base()
        self.designate_repair()
        receipt = dict(version=1, status='passed', outcome='passed', passed=True, sha='b' * 40, platform='windows', kind='full')
        with patch.object(landing, '_land') as land:
            result = self.run_batch(Mock(return_value=receipt))
        self.assertEqual(result['outcome'], 'repair-blocked')
        land.assert_not_called()

    def test_force_full_repair_flag_reaches_ci_preparation(self):
        service = ci.CI('unused-ci-recovery', SimpleNamespace(launch=Mock(return_value=SimpleNamespace(
            poll=lambda: dict(status='timeout', returncode=None, timeout='wall')))), resource_check=resources_go)
        spec = dict(run_id='ci-full-repair', nonce='nonce', placement='wsl')
        with patch.object(ci.repos, 'load', side_effect=lambda prog, *a: prog), \
                patch.object(service, 'prepare', return_value=dict(spec=spec)) as prepare, \
                patch.object(service, 'result', return_value=dict(receipt=None, outcome='infrastructure-failed', reason='timeout', nonce='nonce', run_id=spec['run_id'], excerpt='wall timeout')), \
                patch('drover.disk.read', return_value=[]), patch.object(ci, 'durable'):
            service.run({}, '.', SHA, platforms=['wsl'], force_full=True)
        self.assertTrue(prepare.call_args.kwargs['force_full'])

    def test_base_diagnosis_matches_failed_platform_and_signature(self):
        calls = []
        def gate(work, tip, tasks, *, platform=None, signature=None):
            calls.append((platform, signature))
            return (dict(status='passed', outcome='passed', passed=True, platform=platform) if not tasks else
                    dict(status='failed', passed=False, platform='wsl', failure_signature='runtime-abort'))
        with patch.object(landing, 'requeue'):
            self.assertEqual(self.run_batch(gate)['outcome'], 'red')
        self.assertEqual(calls, [(None, None), ('wsl', 'runtime-abort')])

    def test_base_cache_is_separate_for_platform_and_signature(self):
        calls = []
        def gate(work, tip, tasks, *, platform=None, signature=None):
            calls.append((platform, signature))
            return dict(status='failed' if platform == 'windows' else 'passed')
        result = dict(gates=0, red=0, bisect_gates=0, gate_s=[])
        def status(platform, signature):
            return landing.base_status(self.conn, '.', '.', gate, SHA, result, 'p',
                                       dict(platform=platform, failure_signature=signature))
        self.assertEqual(status('windows', 'suite-a'), 'red')
        self.assertEqual(status('wsl', 'suite-a'), 'green')
        self.assertEqual(status('wsl', 'suite-b'), 'green')
        self.assertEqual(status('windows', 'suite-a'), 'red')
        self.assertEqual(calls, [('windows', 'suite-a'), ('wsl', 'suite-a'), ('wsl', 'suite-b')])

    def test_targeted_green_does_not_clear_other_platform_base_hold(self):
        waiting, cause = self.hold_base()
        def gate(work, tip, tasks, *, platform=None, signature=None):
            return dict(status='passed', outcome='passed', platform=platform)
        result = dict(gates=0, red=0, bisect_gates=0, gate_s=[])
        landing.base_status(self.conn, '.', '.', gate, SHA, result, 'p',
                            dict(platform='wsl', failure_signature='suite-a'))
        self.assertEqual(waiting.state(cause), 'open')
        self.assertTrue(waiting.pending(('merge_queue', 'p')))

    def test_infrastructure_in_base_diagnosis_is_not_cached_as_base_red(self):
        def gate(work, tip, tasks, *, platform=None, signature=None):
            return INFRA if not tasks else dict(status='failed', platform='wsl', failure_signature='suite-a')
        result = self.run_batch(gate)
        self.assertEqual(result['outcome'], 'infra-retry')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM base_diagnoses').fetchone()[0], 0)
        self.assertFalse(Waiting(self.conn).pending(('merge_queue', 'p')))

    def test_owner_retry_invalidates_contextual_base_cache(self):
        from drover.ledger import landing as storage
        storage.record_base(self.conn, 'p', SHA, 'red', 'wsl', 'suite-a')
        self.base_retry()
        calls = []
        def gate(work, tip, tasks, *, platform=None, signature=None):
            calls.append(platform)
            return dict(status='passed' if not tasks else 'failed', platform='wsl', failure_signature='suite-a')
        with patch.object(landing, 'requeue'):
            self.assertEqual(self.run_batch(gate)['outcome'], 'red')
        self.assertEqual(calls, [None, 'wsl'])
        self.assertEqual(storage.base_status(self.conn, 'p', SHA, 'wsl', 'suite-a'), 'green')

    def test_consumed_retry_reopens_question_on_unexpected_gate_exception(self):
        waiting, cause = self.base_retry()
        with self.assertRaisesRegex(RuntimeError, 'broken callback'):
            self.run_batch(Mock(side_effect=RuntimeError('broken callback')))
        self.assertEqual(waiting.state(cause), 'open')
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_admission_queue_preserves_unspent_owner_retry(self):
        waiting, cause = self.base_retry()
        self.assertEqual(self.run_batch(Mock(side_effect=ci.CIDeferred('host busy')))['outcome'], 'queued')
        self.assertEqual(waiting.state(cause), 'authorized')
        self.assertEqual(ops.open_questions(self.conn), [])
        self.assertEqual(self.run_batch(Mock(return_value=INFRA))['outcome'], 'infra-retry')
        self.assertNotEqual(waiting.state(cause), 'attempting')

    def test_resumed_infrastructure_is_retained_for_landing_recovery(self):
        service = ci.CI('unused-ci-recovery', SimpleNamespace(), resource_check=resources_go)
        saved = dict(checkout='work', commit=SHA, kind='full', platforms=['wsl'], base='main')
        with patch.object(Path, 'glob', return_value=[Path('ci-request-key.json')]), \
                patch.object(ci, 'read_result', return_value=saved), patch.object(service, 'run', return_value=INFRA):
            service.resume({}, 'work', terminal_outcomes=True)
            self.assertEqual(service._resumed['work'], ('request-key', INFRA))
            with self.assertRaises(ci.CIInfrastructure):
                service.resume({}, 'work')


class ControlledGitTests(unittest.TestCase):
    def test_controlled_git_guards_merge_update_ref_push_only(self):
        from drover import git_client
        from drover.launch import AdmissionHeld
        guarded, ran = [], []

        def verb(name):
            return lambda repo, *args, **kw: ran.append((name, args)) or name
        with patch.object(landing_control, '_control_guard', side_effect=lambda conn, epoch: guarded.append((conn, epoch))), \
                patch.object(git_client, 'ask', verb('ask')), patch.object(git_client, 'out', verb('out')), \
                patch.object(git_client, 'mutate', verb('mutate')):
            git = landing_control.ControlledGit('conn', 7)
            for name in ('ask', 'out', 'mutate'):
                for args in (('merge', '--ff-only', SHA), ('update-ref', 'refs/heads/main', SHA, SHA), ('push', 'origin'),
                             ('fetch', 'origin'), ('rev-parse', 'HEAD'), ('worktree', 'list'), ('status',), ()):
                    with self.subTest(verb=name, args=args):
                        guarded.clear()
                        self.assertEqual(getattr(git, name)('repo', *args, timeout_s=1), name)
                        self.assertEqual(guarded, [('conn', 7)] if args and args[0] in ('merge', 'update-ref', 'push') else [])
                        self.assertEqual(ran[-1], (name, args))
        ran.clear()
        with patch.object(landing_control, '_control_guard', side_effect=AdmissionHeld('stopped')), \
                patch.object(git_client, 'mutate', verb('mutate')):
            with self.assertRaises(AdmissionHeld):
                landing_control.ControlledGit('conn', 7).mutate('repo', 'merge', '--ff-only', SHA, timeout_s=1)
        self.assertEqual(ran, [])                                   # a held control epoch runs nothing


class LocalFastForwardTests(unittest.TestCase):
    """landing_control.local_fast_forward over origin's real fast_forward, with git faked as test_origin_sync fakes it:
    main moved, a dirty checkout, and a main with no checkout. test_landing_repository keeps the real-git smoke test."""
    def setUp(self):
        from drover import git_client
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.guarded = []
        self.stack = self.enterContext(ExitStack())
        for obj, name, value in ((ci, 'problems', []), (ci, 'validate_receipt', []), (ci, 'head', NEW)):
            self.stack.enter_context(patch.object(obj, name, return_value=value))
        self.stack.enter_context(patch.object(landing_control, '_control_guard', lambda *a, **kw: self.guard(*a, **kw)))
        self.git = None
        for verb in ('ask', 'out', 'mutate'):
            self.stack.enter_context(patch.object(git_client, verb, lambda *a, _verb=verb, **kw: getattr(self.git, _verb)(*a, **kw)))

    def guard(self, conn, epoch=None):
        self.guarded.append(epoch)
        return 7

    def land(self, changes=None):
        self.git = FakeGit(world({('rev-parse', 'refs/heads/main'): (0, OLD + '\n'), **(changes or {})}))
        return landing_control.local_fast_forward(self.conn, REPO, '/work/batch', 'main', NEW, dict(sha=NEW),
                                                  required_platforms=['wsl'])

    def assert_unlocked(self):
        self.assertIsNone(self.conn.execute('SELECT * FROM landing_lock').fetchone())

    def test_moved_main_refuses_to_regate_and_changes_nothing(self):
        for name, changes in (('checkout rewound during the listing', {('rev-parse', 'HEAD'): (0, 'c' * 40 + '\n')}),
                              ('main is no ancestor of the tip', {('merge-base', '--is-ancestor', OLD, NEW): (1, '')})):
            with self.subTest(name):
                with self.assertRaisesRegex(ci.CIError, '^local main moved; rebase and regate$'):
                    self.land(changes)
                self.assertEqual(self.git.mutations(), [])
                self.assert_unlocked()

    def test_dirty_main_checkout_holds_landing_for_the_owner(self):
        for status in (' M base.txt\n', '?? owner.txt\n'):
            with self.subTest(status=status):
                with self.assertRaisesRegex(origin.DirtyCheckout, 'main checkout dirty'):
                    self.land({('status',): (0, status)})
                self.assertEqual(self.git.mutations(), [])
                self.assert_unlocked()

    def test_main_with_no_checkout_moves_by_compare_and_swap_and_leaves_head_alone(self):
        self.land({('worktree', 'list'): (0, listing((REPO, OLD, None)))})
        self.assertEqual(self.git.mutations(), [(REPO, ('update-ref', 'refs/heads/main', NEW, OLD))])
        self.assertFalse([args for _, _, args, _ in self.git.calls if args[0] in ('status', 'merge', 'symbolic-ref')])
        self.assert_unlocked()

    def test_a_held_epoch_stops_the_merge_and_the_compare_and_swap(self):
        from drover.launch import AdmissionHeld
        for name, changes in (('checkout', {}), ('no checkout', {('worktree', 'list'): (0, listing((REPO, OLD, None)))})):
            with self.subTest(name):
                self.guarded.clear()
                held = iter([7])                     # admitted on entry; held by the time main would move
                self.guard = lambda conn, epoch=None: self.guarded.append(epoch) or next(held, None) or _held()
                def _held():
                    raise AdmissionHeld('control stopped')
                with self.assertRaisesRegex(AdmissionHeld, 'control stopped'):
                    self.land(changes)
                self.assertEqual(self.git.mutations(), [])
                self.assertEqual(self.guarded, [None, 7])     # the second check carries the epoch admitted on entry
                self.assert_unlocked()

    def test_every_origin_call_from_landing_control_gets_a_controlled_git(self):
        handed = []

        def record(name, value):
            return lambda *args, git, **kw: handed.append((name, git)) or value
        dirty = '["dirty","p"]'      # the waiting cause of repo p's dirty-checkout hold
        self.git = FakeGit(world({('rev-parse', 'main'): (0, OLD + '\n')}))
        with patch.object(origin, 'sync', record('sync', NEW)), patch.object(origin, 'fast_forward', record('fast_forward', None)), \
                patch('drover.ledger.waiting.Waiting.state', lambda self, cause: 'authorized' if cause == dirty else None),                 patch('drover.ledger.waiting.Waiting.consume_retry', return_value=True):     # the owner authorised a retry
            self.assertIsNone(landing_control.sync_main(self.conn, REPO, 'origin', 'main', repo_name='p'))
        with patch.object(origin, 'sync', record('sync', NEW)), patch.object(origin, 'push_main'), \
                patch.object(landing_control, 'local_fast_forward'):
            landing_control.fast_forward(self.conn, REPO, 'origin', 'main', NEW, dict(sha=NEW))
        self.assertEqual([name for name, _ in handed], ['sync', 'fast_forward', 'sync'])
        for name, git in handed:
            with self.subTest(name):
                self.assertIsInstance(git, landing_control.ControlledGit)
                self.assertEqual((git.conn, git.epoch), (self.conn, 7))
