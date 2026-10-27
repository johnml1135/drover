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
        return landing.run_batch(self.conn, '.', gate, workdir=Path('unused-recovery-work'), repo_name='p',
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
        service = ci.CI('unused-ci-recovery', launcher)
        spec = dict(run_id='ci-queued', nonce='nonce', placement='wsl')
        with patch.object(ci.repos, 'load', side_effect=lambda prog, *a: prog), \
                patch.object(service, 'prepare', return_value=dict(spec=spec)), \
                patch('drover.disk.read', return_value=[]), \
                patch.object(ci.runner, 'write_json_atomic') as save, \
                patch.object(resources, 'check', return_value=dict(verdict='queue', reasons=['load too high'])):
            with self.assertRaisesRegex(ci.CIDeferred, 'load too high'):
                service.run({}, '.', SHA, platforms=['wsl'], conn=self.conn)
        launcher.launch.assert_not_called()
        self.assertEqual(slots.lease_queue(self.conn), [])
        checkpoint = save.call_args.args[1]
        self.assertEqual(checkpoint['execution'], 'prepared')
        self.assertNotIn('ticket', checkpoint)

    def test_resource_deferral_preserves_work_without_spending_recovery(self):
        with patch.object(landing, 'git') as git:
            git.return_value = SimpleNamespace(stdout=SHA, returncode=0)
            result = self.run_batch(Mock(side_effect=ci.CIDeferred('host busy')))
        self.assertEqual(result['outcome'], 'queued')
        self.assertFalse(any(call.args[1:3] == ('worktree', 'remove') for call in git.call_args_list))
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
        receipt = dict(status='passed', passed=True, sha='b' * 40, results=[
            dict(version=1, passed=True, sha='b' * 40, platform=p, kind='full') for p in ('windows', 'wsl')])
        gate = Mock(return_value=receipt)
        with patch.object(landing, 'build_tip', side_effect=lambda repo, work, base, tasks: ('b' * 40, tasks, [])), \
                patch.object(landing, '_land', side_effect=lambda *args: args[6].update(outcome='landed')):
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
        receipt = dict(version=1, status='passed', passed=True, sha='b' * 40, platform='windows', kind='full')
        with patch.object(landing, '_land') as land:
            result = self.run_batch(Mock(return_value=receipt))
        self.assertEqual(result['outcome'], 'repair-blocked')
        land.assert_not_called()

    def test_force_full_repair_flag_reaches_ci_preparation(self):
        service = ci.CI('unused-ci-recovery', SimpleNamespace(launch=Mock(return_value=SimpleNamespace(
            poll=lambda: dict(status='timeout', returncode=None, timeout='wall')))))
        spec = dict(run_id='ci-full-repair', nonce='nonce', placement='wsl')
        with patch.object(ci.repos, 'load', side_effect=lambda prog, *a: prog), \
                patch.object(service, 'prepare', return_value=dict(spec=spec)) as prepare, \
                patch.object(service, 'result', return_value=dict(receipt=None, infra=True, excerpt='wall timeout')), \
                patch('drover.disk.read', return_value=[]), patch.object(ci.runner, 'write_json_atomic'):
            service.run({}, '.', SHA, platforms=['wsl'], force_full=True)
        self.assertTrue(prepare.call_args.kwargs['force_full'])
