"""Landing recovery policy, with in-memory ledger and no external processes."""
import json
import sqlite3
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from drover import ci, landing, resources
from drover.ledger import db, ops
from drover.ledger.waiting import Waiting, BASE, cause_key

SHA = 'a' * 40
INFRA = dict(status='infra', passed=False, infra=True, sha='b' * 40,
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
            (landing, 'git', SimpleNamespace(stdout=SHA, returncode=0)),
            (landing, 'form_batch', [self.task]),
            (landing, 'build_tip', ('b' * 40, [self.task], [])),
            (resources, 'check', dict(verdict='go', reasons=[])),
        ):
            self.stack.enter_context(patch.object(obj, name, return_value=value))
        self.stack.enter_context(patch('drover.ownership.owned_by', return_value=False))
        self.clock = self.stack.enter_context(patch.object(landing.time, 'time', return_value=1000))

    def run_batch(self, gate):
        return landing.run_batch(self.conn, '.', gate, workdir=Path('unused-recovery-work'), repo_name='p')

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
        service = ci.CI('unused-ci-recovery', launcher)
        spec = dict(run_id='ci-timeout', nonce='nonce', placement='wsl')
        with patch.object(ci.repos, 'load', side_effect=lambda prog, *a: prog), \
                patch.object(service, 'prepare', return_value=dict(spec=spec)), \
                patch.object(service, 'result', return_value=dict(receipt=None, infra=True, excerpt='wall timeout')), \
                patch('drover.disk.read', return_value=[]), \
                patch.object(ci.runner, 'write_json_atomic'):
            result = service.run({}, '.', SHA, platforms=['wsl'])
        self.assertEqual((result['status'], result['reason'], result['run_id']),
                         ('infra', 'timeout', 'ci-timeout'))

    def test_budget_survives_batch_membership_and_replayed_tip_changes(self):
        gate = Mock(return_value=INFRA)
        self.run_batch(gate)
        self.clock.return_value = 1100
        other = dict(self.task, id=ops.create_task(self.conn, 'other', 'p')['id'])
        with patch.object(landing, 'form_batch', return_value=[self.task, other]), \
                patch.object(landing, 'build_tip', return_value=('d' * 40, [self.task, other], [])):
            self.assertEqual(self.run_batch(gate)['outcome'], 'infra-held')
        self.assertEqual(gate.call_count, 2)
