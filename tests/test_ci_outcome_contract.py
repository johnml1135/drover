"""Provenance and exact-plan CI decisions without child processes."""
import unittest
from drover import ci_common


class OutcomeContract(unittest.TestCase):
    def setUp(self):
        self.plan = dict(sha='a' * 40, kind='full', platforms=['wsl', 'windows'])
        self.receipts = [dict(version=1, sha=self.plan['sha'], kind='full', platform=p,
                              status='passed', outcome='passed', passed=True, exit_code=0, tree_unchanged=True)
                         for p in self.plan['platforms']]
        self.receipt = {**self.receipts[-1], 'platform': 'wsl+windows', 'results': self.receipts}

    def test_all_required_identity_fields_are_checked(self):
        self.assertEqual(ci_common.validate_receipt(self.receipt, self.plan), [])
        for key, value in [('sha', 'b' * 40), ('kind', 'checks'), ('version', 999), ('platform', 'other')]:
            with self.subTest(field=key):
                wrong = {**self.receipt, 'results': [{**self.receipts[0], key: value}, self.receipts[1]]}
                self.assertTrue(ci_common.validate_receipt(wrong, self.plan))
        self.assertTrue(ci_common.validate_receipt({**self.receipt, 'results': self.receipts[:1]}, self.plan))

    def test_missing_or_wrong_nonce_is_unknown_even_with_a_receipt(self):
        plan = dict(sha='a' * 40, kind='full', platform='wsl', nonce='expected', run_id='r')
        for status in ({}, {'state': 'exited', 'nonce': 'other', 'exit_code': 0, 'run_id': 'r'}):
            with self.subTest(status=status):
                result = ci_common.evaluate_result(self.receipts[0], plan, status, {'status': 'done', 'returncode': 0}, [])
                self.assertEqual(result['outcome'], 'termination-unknown')

    def test_status_without_run_id_or_plan_without_nonce_never_passes(self):
        plan = dict(sha='a' * 40, kind='full', platform='wsl', nonce='n', run_id='r')
        receipt = {**self.receipts[0], 'nonce': 'n', 'run_id': 'r'}
        run = {'status': 'done', 'returncode': 0}
        good = {'state': 'exited', 'nonce': 'n', 'run_id': 'r', 'exit_code': 0}
        self.assertEqual(ci_common.evaluate_result(receipt, plan, good, run, [])['outcome'], 'passed')
        self.assertEqual(ci_common.evaluate_result(receipt, plan, {k: v for k, v in good.items() if k != 'run_id'}, run, [])['outcome'],
                         'termination-unknown')
        bare = {k: v for k, v in plan.items() if k != 'nonce'}
        self.assertEqual(ci_common.evaluate_result({k: v for k, v in receipt.items() if k != 'nonce'}, bare, good, run, [])['outcome'],
                         'termination-unknown')
        self.assertEqual(ci_common.evaluate_result({k: v for k, v in receipt.items() if k != 'run_id'}, plan, good, run, [])['outcome'],
                         'termination-unknown')
        self.assertEqual(ci_common.evaluate_result({**receipt, 'nonce': 'x'}, plan, good, run, [])['outcome'], 'termination-unknown')

    def test_landing_batch_keys_infrastructure_on_typed_outcome(self):
        from drover import ci, landing_batch
        receipt = dict(outcome='infrastructure-failed', status='failed', passed=False)
        with self.assertRaises(ci.CIInfrastructure):
            landing_batch._gate(lambda *a: receipt, '.', 'a' * 40, [], {'gate_s': [], 'gates': 0, 'bisect_gates': 0, 'red': 0})


from pathlib import Path
from unittest.mock import Mock, patch
from drover import runner
from drover.ledger import ops
from tests.test_pipeline import PolicyBase


class DispatcherOutcomeContract(PolicyBase):
    def start_gate(self):
        task = self.task()
        self.d.tick()
        sha = self.worker_commits(task)
        self.run_worker(task, sha)
        self.d.tick()
        spec = self.fl.of('gate')[-1]
        return task, spec, runner.run_dir(self.runs, spec['run_id'])

    def test_missing_and_wrong_nonce_terminal_status_hold_run_and_capacity(self):
        import json
        task, spec, directory = self.start_gate()
        original = json.loads((directory / runner.STATUS_FILE).read_text())
        for value in (None, {**original, 'nonce': 'other'}):
            if value is None:
                (directory / runner.STATUS_FILE).unlink(missing_ok=True)
            else:
                runner.write_json_atomic(directory / runner.STATUS_FILE, value)
            for _ in range(3):
                self.d.tick()
            self.assertEqual(len(self.fl.of('gate')), 1)
            self.assertIsNone(ops.list_runs(self.conn, task)[1]['ended'])
            self.assertIn(task, self.d.ci_runners.tickets)
        runner.write_json_atomic(directory / runner.STATUS_FILE, original)
        self.d.tick()
        self.assertEqual(ops.list_runs(self.conn, task)[0]['outcome'], 'gated')

    def test_wrong_receipt_identity_never_gates_the_worker(self):
        import json
        for key, value in [('sha', 'f' * 40), ('platform', 'other'), ('kind', 'full'), ('version', 999)]:
            task, spec, directory = self.start_gate()
            path = directory / 'receipt.json'
            receipt = json.loads(path.read_text())
            path.write_text(json.dumps({**receipt, key: value}))
            self.d.tick()
            self.assertNotEqual(ops.list_runs(self.conn, task)[0]['outcome'], 'gated', key)
            self.assertEqual(self.status(task), 'needs-owner')

    def test_infrastructure_budget_and_owner_continue_create_a_new_episode(self):
        self.fl.gate_script = [('timeout', 1, '')] * 4
        task, _, _ = self.start_gate()
        for _ in range(5):
            self.d.tick()
        questions = ops.open_questions(self.conn)
        self.assertEqual(len(questions), 1)
        self.assertEqual(len(self.fl.of('gate')), 2)
        self.assertEqual(len(self.fl.of('worker')), 1)
        self.assertIn('timed out', questions[0]['body'])
        self.assertIn('platform windows', questions[0]['body'])
        self.assertIn('nonce ', questions[0]['body'])
        with self.assertRaises(ValueError):
            ops.answer_question(self.conn, questions[0]['id'], text='continue')
        ops.answer_question(self.conn, questions[0]['id'], choice='Retry on a healthy host')
        for _ in range(5):
            self.d.tick()
        self.assertEqual(len(self.fl.of('worker')), 1)
        self.assertEqual(len(self.fl.of('gate')), 4)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertEqual(len(ops.events(self.conn, task_id=task, kind='gate-infra-continue')), 1)

    def test_automatic_infrastructure_retry_waits_for_healthy_placement(self):
        self.fl.gate_script = [('timeout', 1, '')]
        task, spec, _ = self.start_gate()
        self.d.reap()
        self.d.resources_fn = lambda **_: {'verdict': 'go', 'slots': 10, 'reasons': [],
            'side_admission': {spec['placement']: {'verdict': 'hold'}}}
        self.d.tick()
        self.assertEqual(len(self.fl.of('gate')), 1)
        self.assertEqual(ops.list_runs(self.conn, task)[0]['outcome'], 'done')
        self.d.resources_fn = lambda **_: {'verdict': 'go', 'slots': 10, 'reasons': []}
        self.d.tick()
        self.assertEqual(len(self.fl.of('gate')), 2)

    def test_failed_finish_keeps_the_reservation_for_retry(self):
        self.fl.gate_script = [('timeout', 1, '')]
        task, spec, _ = self.start_gate()
        append = self.port.append_event
        def fail_once(kind, *args, **kwargs):
            if kind == 'gate-infra-retry':
                raise RuntimeError('ledger unavailable')
            return append(kind, *args, **kwargs)
        with patch.object(self.port, 'append_event', side_effect=fail_once):
            self.d.reap()
        self.assertIsNone(ops.list_runs(self.conn, task)[1]['ended'])
        self.assertIn(task, self.d.ci_runners.tickets)
        self.d.reap()
        self.assertIsNotNone(ops.list_runs(self.conn, task)[1]['ended'])
        self.assertNotIn(task, self.d.ci_runners.tickets)

    def test_owner_can_keep_infrastructure_hold_without_relaunch(self):
        self.fl.gate_script = [('timeout', 1, '')] * 3
        task, _, _ = self.start_gate()
        for _ in range(4):
            self.d.tick()
        question, = ops.open_questions(self.conn)
        ops.answer_question(self.conn, question['id'], choice='Keep holding')
        for _ in range(3):
            self.d.tick()
        self.assertEqual(len(self.fl.of('gate')), 2)
        self.assertEqual(len(self.fl.of('worker')), 1)
        self.assertEqual(ops.get_question(self.conn, question['id'])['status'], 'open')
        self.assertNotEqual(self.status(task), 'gated')

    def test_failed_module_and_interrupted_text_returns_to_worker(self):
        import json
        from drover import ci, placement
        from tests.fakeci import FakeCommands
        from tests.test_pipeline import FakeHandle
        fake = FakeCommands(Path(self.tmp.name) / 'command')
        normal_launch = self.fl.launch
        def launch(spec):
            if spec.get('role') != 'gate':
                return normal_launch(spec)
            directory = runner.run_dir(self.runs, spec['run_id'])
            plan = json.loads((directory / 'plan.json').read_text())
            (directory / runner.OUT_FILE).write_text(
                'CIInterrupted: termination-uncertain: test subject text\n'
                'DROVER_CI_FAILURE default tests.test_example\n')
            fake.expect(plan['commands'][0], cwd=plan['repo'], env=fake.env, returncode=1)
            with patch.object(ci, 'subprocess', fake), patch.object(ci, 'git_environment', return_value=fake.env), \
                    patch.object(ci, 'head', return_value=plan['sha']), patch.object(ci, 'dirty', return_value=False), \
                    patch.object(ci, 'contains', return_value=(plan['sha'], True)), \
                    patch.object(placement, 'prepare_worktree'), patch.object(placement, 'ci_command', side_effect=lambda a, _: a):
                self.assertEqual(ci._execute(plan), 1)
            fake.assert_done()
            runner.write_json_atomic(directory / runner.STATUS_FILE,
                                     dict(state='exited', exit_code=1, nonce=spec['nonce'], run_id=spec['run_id']))
            value = dict(status='error', returncode=1, record=None, text='', timeout=None,
                         usage=dict(tokens_in=0, tokens_out=0, tokens_cached=0, context=0))
            self.fl.specs.append(spec)
            self.fl.handles[spec['run_id']] = FakeHandle(value)
            return self.fl.handles[spec['run_id']]
        self.fl.launch = launch
        task, _, directory = self.start_gate()
        self.assertEqual(json.loads((directory / 'receipt.json').read_text())['module'], 'tests.test_example')
        self.d.tick()
        self.assertEqual(ops.list_runs(self.conn, task)[0]['outcome'], 'gate-fail')
        self.assertIn('tests.test_example', self.fl.of('worker')[-1]['user_prompt'])
        self.assertEqual(ops.events(self.conn, task_id=task, kind='gate-infra-retry'), [])


from tests import test_landing_recovery as recovery_fixture
from drover import landing


class LandingOutcomeContract(unittest.TestCase):
    setUp = recovery_fixture.RecoveryTests.setUp
    run_batch = recovery_fixture.RecoveryTests.run_batch
    def test_wrong_receipt_identity_never_lands(self):
        base = dict(version=1, sha='b' * 40, kind='full', status='passed', outcome='passed', passed=True,
                    exit_code=0, tree_unchanged=True)
        rows = [{**base, 'platform': p} for p in ('windows', 'wsl')]
        receipt = {**base, 'platform': 'windows+wsl', 'results': rows}
        for key, value in [('sha', 'd' * 40), ('platform', 'other'), ('kind', 'checks'), ('version', 999)]:
            bad = {**receipt, 'results': [{**rows[0], key: value}, rows[1]]}
            with patch.object(landing, '_land') as land:
                result = self.run_batch(Mock(return_value=bad))
            self.assertEqual(result['outcome'], 'refused', key)
            land.assert_not_called()


class DeployOutcomeContract(unittest.TestCase):
    def test_wrong_identity_never_deploys(self):
        import importlib.util
        import json
        import tempfile
        from types import SimpleNamespace
        from drover import repos
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'deploy_hub.py'
        spec = importlib.util.spec_from_file_location('deploy_ci_contract', script)
        deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy)
        sha = 'a' * 40
        base = dict(version=1, sha=sha, kind='full', status='passed', outcome='passed', passed=True,
                    exit_code=0, tree_unchanged=True)
        rows = [{**base, 'platform': p} for p in ('windows', 'wsl')]
        receipt = {**base, 'platform': 'windows+wsl', 'results': rows}
        with tempfile.TemporaryDirectory() as root, patch.object(deploy, 'git', return_value=SimpleNamespace(returncode=0)), \
                patch.object(repos, 'load', return_value={'gate': {'platform': 'both'}}):
            path = Path(root) / 'receipt.json'
            path.write_text(json.dumps(receipt))
            self.assertEqual(deploy.check_commit(root, sha, 'main', path, False), [])
            for key, value in [('sha', 'd' * 40), ('platform', 'other'), ('kind', 'checks'), ('version', 999)]:
                path.write_text(json.dumps({**receipt, 'results': [{**rows[0], key: value}, rows[1]]}))
                self.assertTrue(deploy.check_commit(root, sha, 'main', path, False), key)


class FakeCommandProvenance(unittest.TestCase):
    def test_failed_command_log_cannot_claim_infrastructure(self):
        import json
        import tempfile
        from drover import ci, placement
        from tests.fakeci import FakeCommands
        with tempfile.TemporaryDirectory() as root:
            fake = FakeCommands(root)
            log, out = Path(root) / 'out.log', Path(root) / 'receipt.json'
            log.write_text('CIInterrupted: termination-uncertain: from the test\n'
                           'DROVER_CI_FAILURE default tests.test_example\n')
            plan = dict(repo=str(fake.cwd), sha='a' * 40, base='main', kind='checks', platform='wsl',
                        commands=[['test-command']], out=str(out), log=str(log), run_id='run', nonce='nonce')
            fake.expect(['test-command'], cwd=str(fake.cwd), env=fake.env, returncode=1)
            with patch.object(ci, 'subprocess', fake), patch.object(ci, 'git_environment', return_value=fake.env), \
                    patch.object(ci, 'head', return_value=plan['sha']), patch.object(ci, 'dirty', return_value=False), \
                    patch.object(ci, 'contains', return_value=('b' * 40, True)), \
                    patch.object(placement, 'prepare_worktree'), patch.object(placement, 'ci_command', side_effect=lambda a, _: a):
                self.assertEqual(ci._execute(plan), 1)
            fake.assert_done()
            receipt = json.loads(out.read_text())
            result = ci_common.evaluate_result(receipt, plan, dict(state='exited', nonce='nonce', run_id='run', exit_code=1),
                                               dict(status='error', returncode=1), [])
            self.assertEqual(result['outcome'], 'code-failed')
            self.assertEqual(receipt['module'], 'tests.test_example')
            self.assertNotEqual(result['outcome'], 'infrastructure-failed')

    def test_five_outcomes_have_distinct_process_provenance(self):
        plan = dict(sha='a' * 40, kind='checks', platform='wsl', nonce='n', run_id='r')
        receipt = dict(version=1, sha=plan['sha'], kind='checks', platform='wsl', nonce='n', run_id='r',
                       passed=True, status='passed', outcome='passed', exit_code=0, tree_unchanged=True)
        status = dict(state='exited', nonce='n', run_id='r', exit_code=0)
        execution = dict(status='done', returncode=0)
        cases = [
            (receipt, status, execution, 'passed'),
            ({**receipt, 'passed': False, 'status': 'failed', 'exit_code': 1},
             {**status, 'exit_code': 1}, {'status': 'error', 'returncode': 1}, 'code-failed'),
            (None, {**status, 'state': 'timeout'}, execution, 'infrastructure-failed'),
            (receipt, {}, execution, 'termination-unknown'),
            ({**receipt, 'status': 'refused'}, status, execution, 'deferred'),
        ]
        for row, supervisor, process, expected in cases:
            with self.subTest(expected=expected):
                result = ci_common.evaluate_result(row, plan, supervisor, process, [])
                self.assertEqual(result['outcome'], expected)
