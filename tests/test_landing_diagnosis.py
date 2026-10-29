"""Landing diagnostic and origin-liveness regressions."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from drover import ci, landing, landing_control
from drover.ledger import ops
from tests import test_landing_recovery as fixture
SHA, INFRA = fixture.SHA, fixture.INFRA

class LandingDiagnosis(unittest.TestCase):
    setUp = fixture.RecoveryTests.setUp
    run_batch = fixture.RecoveryTests.run_batch

    def test_exhausted_recovery_question_names_every_affected_task(self):
        other = dict(self.task, id=ops.create_task(self.conn, 'other', 'p')['id'])
        with patch.object(landing, 'form_batch', return_value=[self.task, other]), \
                patch.object(landing, 'build_tip', return_value=('b' * 40, [self.task, other], [])):
            self.run_batch(Mock(return_value=INFRA))
            self.clock.return_value = 1100
            self.run_batch(Mock(return_value=INFRA))
            self.run_batch(Mock(return_value=INFRA))
        question, = ops.open_questions(self.conn)
        self.assertIn('timeout', question['body'])
        self.assertIn(self.task['id'], question['body'])
        self.assertIn(other['id'], question['body'])

    def test_remote_move_is_observed_during_gate_and_skips_old_base_diagnosis(self):
        import threading
        started, observed = threading.Event(), threading.Event()
        calls = []
        def observe(repo, remote, branch, stopped, moved, tracking, interval):
            if started.wait(2):
                moved.set()
                observed.set()
        def gate(work, tip, tasks):
            calls.append((tip, bool(tasks)))
            started.set()
            self.assertTrue(observed.wait(2), 'remote move was not observed while CI was running')
            return dict(status='failed', platform='wsl', failure_signature='suite-a')
        with patch.object(landing.origin, 'observe', side_effect=observe, create=True), \
                patch.object(landing, 'ORIGIN_OBSERVE_INTERVAL_S', .001, create=True), \
                patch.object(landing, 'base_status') as diagnosis:
            result = landing.run_batch(self.conn, '.', gate, workdir=Path('unused-recovery-work'), repo_name='p',
                                       observe=landing.origin.observe)
        self.assertEqual(result['outcome'], 'stale-base')
        self.assertEqual(calls, [('b' * 40, True)])
        diagnosis.assert_not_called()

    def test_next_pass_rebuilds_and_regates_on_advanced_base(self):
        new_sha = 'd' * 40
        state = {'sha': SHA, 'syncs': 0}
        built, gated = [], []
        def sync(*args, **kwargs):
            state['syncs'] += 1
            if state['syncs'] > 1:
                state['sha'] = new_sha
        def git(repo, *args, **kwargs):
            return SimpleNamespace(stdout=state['sha'], returncode=0)
        def build(repo, work, base, tasks):
            built.append(base)
            return ('b' if base == SHA else 'e') * 40, tasks, []
        def gate(work, tip, tasks):
            gated.append(tip)
            return dict(status='failed' if len(gated) == 1 else 'passed', platform='wsl')
        def observe(repo, remote, branch, stopped, moved, tracking, interval):
            if tracking == SHA:
                moved.set()
        def land(conn, repo, work, tasks, tip, receipt, result, *args):
            result.update(outcome='landed', landed=[self.task['id']])
        with patch.object(landing, 'sync_main', side_effect=sync), patch.object(landing, 'git', side_effect=git), \
                patch.object(landing, 'build_tip', side_effect=build), patch.object(landing, '_land', side_effect=land):
            self.assertEqual(landing.run_batch(self.conn, '.', gate, workdir='unused-recovery-work',
                                               repo_name='p', observe=observe)['outcome'], 'stale-base')
            self.assertEqual(landing.run_batch(self.conn, '.', gate, workdir='unused-recovery-work',
                                               repo_name='p', observe=observe)['outcome'], 'landed')
        self.assertEqual(built, [SHA, new_sha])
        self.assertEqual(gated, ['b' * 40, 'e' * 40])

    def test_origin_observer_reads_remote_without_mutating_refs(self):
        import threading
        stopped, moved = threading.Event(), threading.Event()
        remote_sha = 'd' * 40
        response = SimpleNamespace(returncode=0, stdout=f'{remote_sha}\trefs/heads/main\n')
        with patch.object(landing.origin, 'git', return_value=response) as git:
            landing.origin.observe('.', 'origin', 'main', stopped, moved, SHA, 120)
        self.assertTrue(moved.is_set())
        self.assertEqual(git.call_args.args, ('.', 'ls-remote', '--heads', 'origin', 'refs/heads/main'))

    def test_named_failed_module_is_forwarded_to_exact_base_diagnosis(self):
        calls = []
        def gate(work, tip, tasks, *, platform=None, signature=None, module=None, tier=None):
            calls.append((tip, platform, signature, module, tier))
            return dict(status='passed')
        result = dict(gates=0, red=0, bisect_gates=0, gate_s=[])
        failure = dict(platform='wsl', failure_signature='failure-a', module='tests.test_ci', tier='integration')
        with patch.object(landing_control, 'build_tip', return_value=(SHA, [], [])):
            self.assertEqual(landing.base_status(self.conn, '.', '.', gate, SHA, result, 'p', failure), 'green')
        self.assertEqual(calls, [(SHA, 'wsl', 'failure-a', 'tests.test_ci', 'integration')])

    def test_diagnostic_cache_reuses_only_identical_module_tier_and_signature(self):
        calls = []
        def gate(work, tip, tasks, *, platform=None, signature=None, module=None, tier=None):
            calls.append((platform, signature, module, tier))
            return {'status': 'passed'}
        result = dict(gates=0, red=0, bisect_gates=0, gate_s=[])
        def diagnose(module, tier):
            return landing.base_status(self.conn, '.', '.', gate, SHA, result, 'p',
                dict(platform='wsl', failure_signature='same', module=module, tier=tier))
        with patch.object(landing_control, 'build_tip', return_value=(SHA, [], [])):
            self.assertEqual(diagnose('tests.test_ci', 'integration'), 'green')
            self.assertEqual(diagnose('tests.test_ci', 'integration'), 'green')
            self.assertEqual(diagnose('tests.test_ci', 'default'), 'green')
            self.assertEqual(diagnose('tests.test_other', 'integration'), 'green')
        self.assertEqual(len(calls), 3)

    def test_termination_uncertainty_is_recorded_without_blame_or_base_question(self):
        error = ci.CIInterrupted('termination-uncertain: missing nonce-matched runner status')
        with patch.object(landing, 'base_status') as diagnosis, patch.object(landing, 'requeue') as blame:
            with self.assertRaises(ci.CIInterrupted):
                self.run_batch(Mock(side_effect=error))
        event = ops.events(self.conn, kind='landing.outcome')[-1]
        self.assertEqual(event['task_id'], self.task['id'])
        self.assertEqual(event['data']['outcome'], 'infra-uncertain')
        self.assertIn('missing nonce', event['data']['reason'])
        self.assertEqual(ops.open_questions(self.conn), [])
        diagnosis.assert_not_called()
        blame.assert_not_called()

