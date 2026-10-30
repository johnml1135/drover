"""Gate ordering and host admission policy: fake execution, no child processes."""
import json
import sqlite3
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from drover import ci, dispatcher, launch, runner
from drover.ledger import ci as slots, db, ops
from drover.port import DirectPort
from tests.fakeci import resources_go

def memory_ledger(migrate=True):
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    if migrate:
        db.migrate(conn)
    return conn

class WindowsCapacity(unittest.TestCase):
    def setUp(self):
        self.conn = memory_ledger()
        self.addCleanup(self.conn.close)

    def request(self, name, platform='windows', priority='P2'):
        return slots.lease_request(self.conn, name, priority, platform=platform)

    def acquire(self, ticket, limit=1, at=None):
        return slots.lease_acquire(self.conn, ticket, slots=5, reserved=1, windows_slots=limit, at=at)

    def test_windows_cap_leaves_room_for_wsl_even_when_windows_has_priority(self):
        first = self.request('first')
        self.assertIsNotNone(self.acquire(first))
        top = self.request('top', priority='P0')
        wsl = self.request('wsl', 'wsl')
        self.assertIsNone(self.acquire(top))
        self.assertIsNotNone(self.acquire(wsl))
        slots.lease_release(self.conn, first)
        self.assertIsNotNone(self.acquire(top))

    def test_configurable_cap_counts_ranked_waiters_and_preserves_reserve(self):
        tickets = [self.request(str(i)) for i in range(3)]
        self.assertIsNone(self.acquire(tickets[2], limit=2))
        self.assertIsNotNone(self.acquire(tickets[0], limit=2))
        self.assertIsNotNone(self.acquire(tickets[1], limit=2))
        self.assertIsNone(self.acquire(tickets[2], limit=2))
        self.assertIsNotNone(self.acquire(tickets[2], limit=3))
        low = self.request('low', 'wsl')
        self.assertIsNotNone(self.acquire(low, limit=3))
        self.assertIsNone(self.acquire(self.request('overflow', 'wsl'), limit=3))
        self.assertEqual(self.acquire(self.request('urgent', priority='P1'), limit=4), 4)

    def test_pinned_windows_execution_counts_after_ttl_and_lowered_limit(self):
        first, second = self.request('first'), self.request('second')
        self.assertIsNotNone(self.acquire(first, limit=2))
        self.assertIsNotNone(self.acquire(second, limit=2))
        slots.lease_pin(self.conn, first)
        slots.lease_pin(self.conn, second)
        future = time.time() + 1000
        third = self.request('third')
        slots.lease_heartbeat(self.conn, third, at=future)
        self.assertIsNotNone(self.acquire(second, at=future))  # adopt despite lowered limit
        self.assertIsNone(self.acquire(third, at=future))
        slots.lease_release(self.conn, first)
        self.assertIsNone(self.acquire(third, at=future))
        slots.lease_release(self.conn, second)
        self.assertIsNotNone(self.acquire(third, at=future))

    def test_setting_defaults_clamps_and_logs_invalid_values_once(self):
        self.assertEqual(slots.configured_windows_limit(lambda k, d: d), 1)
        for raw, expected in [('0', 1), ('99', 32), ('2', 2), ('', 1)]:
            self.assertEqual(slots.configured_windows_limit(lambda k, d: raw), expected)
        with patch('sys.stderr') as err:
            for _ in range(2):
                self.assertEqual(slots.configured_windows_limit(lambda k, d: 'invalid-q45'), 1)
            self.assertEqual(sum('gate_windows_slots' in str(c) for c in err.write.call_args_list), 1)

    def test_dispatcher_and_full_ci_share_the_same_windows_capacity(self):
        runners = slots.Runners(self.conn)
        task = {'id': 'task', 'priority': 'P2'}
        self.assertIsNotNone(runners.reserve(task, platform='windows'))
        self.assertIsNone(runners.reserve({'id': 'another-windows', 'priority': 'P2'}, platform='windows'))
        full = self.request('full', priority='P1')
        self.assertIsNone(self.acquire(full))
        self.assertIsNotNone(runners.reserve({'id': 'wsl', 'priority': 'P2'}, platform='wsl'))
        runners.release('task')
        self.assertIsNotNone(self.acquire(full))

    def test_dispatcher_restart_restores_live_ticket_until_terminal_release(self):
        task = ops.create_task(self.conn, 'task', 'p', scope_files=['fixture-96'])
        run_id = ops.start_run(self.conn, task['id'], 'gate', 'local')
        runners = slots.Runners(self.conn)
        self.assertIsNotNone(runners.reserve(task))
        runners.pin(task['id'])
        self.conn.execute('UPDATE gate_tickets SET heartbeat=0')
        restarted = slots.Runners(self.conn)
        full = self.request('full')
        self.assertIsNone(self.acquire(full))
        restarted.heartbeat({task['id']})
        self.assertEqual(slots.lease_queue(self.conn)[0]['pinned'], 1)
        ops.finish_run(self.conn, run_id, 'passed')
        slots.Runners(self.conn)  # crash after finish, before explicit release
        self.assertIsNotNone(self.acquire(full))

    def test_cancelled_dispatcher_launch_keeps_ticket_until_reconciled(self):
        task = ops.create_task(self.conn, 'task', 'p', scope_files=['fixture-112'])
        run_id = ops.start_run(self.conn, task['id'], 'gate', 'local')
        runners = slots.Runners(self.conn)
        self.assertIsNotNone(runners.reserve(task))
        runners.pin(task['id'])
        ops.finish_run(self.conn, run_id, 'launch-error')
        ops.event(self.conn, 'launch-cancel', 'dispatcher', {'run': run_id}, task_id=task['id'])
        slots.Runners(self.conn)
        full = self.request('full')
        self.assertIsNone(self.acquire(full))
        ops.event(self.conn, 'launch-cancel-cleared', 'dispatcher', {'run': run_id}, task_id=task['id'])
        slots.Runners(self.conn)
        self.assertIsNotNone(self.acquire(full))

    def test_v15_migration_conservatively_counts_old_tickets(self):
        from drover.ledger import db
        from drover.ledger.schema import MIGRATIONS
        old = memory_ledger(migrate=False)
        self.addCleanup(old.close)
        db.migrate(old, MIGRATIONS[:14])
        old.execute("INSERT INTO gate_tickets(task_id,rank,holder,slot,requested,heartbeat,pinned) "
                    "VALUES('old',2,'ci-old',0,0,0,1)")
        db.migrate(old)
        row, = slots.lease_queue(old)
        self.assertEqual((row['platform'], row['slot'], row['pinned']), ('windows', 0, 1))
        ticket = slots.lease_request(old, 'new', 'P1')
        self.assertIsNone(slots.lease_acquire(old, ticket, windows_slots=1))
        db.migrate(old)
        self.assertEqual(len(slots.lease_queue(old)), 2)


class GateLaunchCancellation(unittest.TestCase):
    def setUp(self):
        self.conn = memory_ledger()
        self.addCleanup(self.conn.close)
        self.port = DirectPort(self.conn)
        self.task = ops.create_task(self.conn, 'task', 'p', scope_files=['fixture-148'])
        ops.approve(self.conn, self.task['id'])
        ops.claim_next(self.conn)
        worker = ops.start_run(self.conn, self.task['id'], 'worker', 'local')
        ops.finish_run(self.conn, worker, 'blocked')
        self.launcher = SimpleNamespace(scan=Mock(return_value={}), stop_run=Mock())
        self.d = self.dispatcher()

    def dispatcher(self):
        d = dispatcher.Dispatcher(self.port, SimpleNamespace(port=self.port),
                                  dispatcher.DispatchRuntime(launcher=self.launcher))
        d.scan_every_s = 0
        return d

    def gate(self):
        run_id = self.port.start_run(self.task['id'], 'gate', 'local')
        self.assertIsNotNone(self.d.ci_runners.reserve(self.task))
        self.d.ci_runners.pin(self.task['id'])
        self.port.append_event('run-launching', {'run': run_id, 'placement': 'windows', 'nonce': 'nonce'}, self.task['id'])
        return run_id

    def cancel(self, run_id):
        self.d._launch_error(self.task, run_id, {'backend': 'local'}, 'windows',
                             launch.LaunchError('still starting', cancel_requested=True), 'nonce', role='gate')

    def test_third_cancelled_gate_launch_is_tracked_until_reconciled(self):
        for _ in range(2):
            self.port.finish_run(self.port.start_run(self.task['id'], 'gate', 'local'), 'launch-error')
        run_id = self.gate()
        self.cancel(run_id)
        self.assertEqual(self.port.get_task(self.task['id'])['status'], 'needs-owner')
        self.assertIn(run_id, self.d.cancelling)
        self.assertEqual(self.port.events('launch-cancel')[0]['data']['run'], run_id)
        self.assert_reconciled(run_id)

    def test_restart_reconciles_cancelled_gate_in_needs_owner(self):
        for _ in range(2):
            self.port.finish_run(self.port.start_run(self.task['id'], 'gate', 'local'), 'launch-error')
        run_id = self.gate()
        self.cancel(run_id)
        self.d = self.dispatcher()
        self.d.recover_orphans()
        self.assertIn(run_id, self.d.cancelling)
        self.assert_reconciled(run_id)

    def test_crash_after_finish_before_cancel_event_retains_reservation(self):
        run_id = self.gate()
        append = self.port.append_event
        def crash(kind, *args, **kwargs):
            if kind == 'launch-cancel':
                raise RuntimeError('hub crashed')
            return append(kind, *args, **kwargs)
        with patch.object(self.port, 'append_event', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'hub crashed'):
                self.cancel(run_id)
        self.assertEqual(self.port.list_runs(self.task['id'])[-1]['outcome'], 'launch-error')
        self.assertEqual(self.port.events('launch-cancel'), [])
        self.d = self.dispatcher()
        self.d.recover_orphans()
        self.assertIn(run_id, self.d.cancelling)
        self.assert_reconciled(run_id)

    def assert_reconciled(self, run_id):
        full = slots.lease_request(self.conn, 'full', 'P1')
        self.launcher.scan.side_effect = runner.ScanError('unknown')
        self.d.watch_runs()
        self.assertIsNone(slots.lease_acquire(self.conn, full, windows_slots=1))
        self.launcher.scan.side_effect = None
        self.launcher.scan.return_value = {run_id: 'windows'}
        self.d.watch_runs()
        self.launcher.stop_run.assert_called_with(run_id, 'nonce')
        self.assertIsNone(slots.lease_acquire(self.conn, full, windows_slots=1))
        self.launcher.scan.return_value = {}
        self.d.watch_runs()
        self.assertNotIn(run_id, self.d.cancelling)
        self.assertNotIn(self.task['id'], self.d.ci_runners.tickets)
        self.assertIsNotNone(slots.lease_acquire(self.conn, full, windows_slots=1))
        restarted = self.dispatcher()
        restarted.recover_orphans()
        self.assertNotIn(run_id, restarted.cancelling)


class FullGateOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = memory_ledger()
        self.addCleanup(self.conn.close)
        self.calls, self.outcomes = [], {}
        self.interrupt = None
        self.launcher = SimpleNamespace(launch=self.launch, adopt=self.adopt)
        self.service = ci.CI(self.tmp.name, self.launcher, poll_s=0, resource_check=resources_go)
        self.load = patch.object(ci.repos, 'load', side_effect=lambda prog, *a: prog)
        self.load.start()
        self.addCleanup(self.load.stop)
        disk = patch('drover.disk.read', return_value=[])
        disk.start()
        self.addCleanup(disk.stop)
        prepare = patch.object(ci.CI, 'prepare', side_effect=self.prepare)
        prepare.start()
        self.addCleanup(prepare.stop)

    def prepare(self, prog, checkout, commit, run_id, *, kind, platform, **kw):
        runner.run_dir(self.tmp.name, run_id).mkdir(parents=True, exist_ok=True)
        runner.write_json_atomic(runner.run_dir(self.tmp.name, run_id) / 'plan.json', dict(sha=commit, kind=kind, platform=platform, run_id=run_id, nonce=run_id))
        return {'spec': {'run_id': run_id, 'nonce': run_id, 'placement': platform}, 'receipt': ''}

    def finish(self, spec):
        passed = self.outcomes.get(spec['placement'], True)
        d = runner.run_dir(self.tmp.name, spec['run_id'])
        runner.write_json_atomic(d / 'receipt.json', {'version': 1, 'sha': 'exact-sha', 'platform': spec['placement'],
                                'nonce': spec['nonce'], 'run_id': spec['run_id'], 'tree_unchanged': True, 'kind': self.kind, 'passed': passed, 'exit_code': 0 if passed else 1,
                                'status': 'passed' if passed else 'failed', 'outcome': 'passed' if passed else 'code-failed'})
        runner.write_json_atomic(d / runner.STATUS_FILE, {'state': 'exited', 'nonce': spec['nonce'], 'run_id': spec['run_id'], 'exit_code': 0 if passed else 1})
        return SimpleNamespace(poll=lambda: {'returncode': 0 if passed else 1, 'status': 'done'})

    def launch(self, spec):
        self.calls.append(('launch', spec['placement'], spec['run_id']))
        if self.interrupt == spec['placement']:
            runner.write_json_atomic(runner.run_dir(self.tmp.name, spec['run_id']) / runner.STATUS_FILE,
                                     {'state': 'running', 'nonce': spec['nonce']})
            raise RuntimeError('lost response')
        return self.finish(spec)

    def adopt(self, spec):
        self.calls.append(('adopt', spec['placement'], spec['run_id']))
        return self.finish(spec)

    def run_gate(self, kind='full'):
        self.kind = kind
        return self.service.run({}, 'checkout', 'exact-sha', platforms=['windows', 'wsl'], kind=kind, conn=self.conn)

    def test_checks_and_full_keep_each_platform_receipt_for_exact_sha(self):
        for kind in ('checks', 'full'):
            self.calls.clear()
            with patch.object(slots, 'lease_acquire', wraps=slots.lease_acquire) as acquire:
                result = self.run_gate(kind)
            self.assertTrue(all(call.kwargs['windows_slots'] == 1 for call in acquire.call_args_list))
            self.assertTrue(result['passed'])
            self.assertEqual([c[1] for c in self.calls], ['wsl', 'windows'])
            self.assertEqual([(r['platform'], r['sha'], r['kind']) for r in result['results']],
                             [('wsl', 'exact-sha', kind), ('windows', 'exact-sha', kind)])
            self.assertEqual(slots.lease_queue(self.conn), [])

    def test_either_failure_fails_gate_and_wsl_failure_skips_windows(self):
        for platform, expected in [('wsl', ['wsl']), ('windows', ['wsl', 'windows'])]:
            self.calls.clear()
            self.outcomes = {platform: False}
            result = self.run_gate()
            self.assertFalse(result['passed'])
            self.assertEqual(result['platform'], platform)
            self.assertEqual([c[1] for c in self.calls], expected)
            self.assertEqual(slots.lease_queue(self.conn), [])

    def test_restart_adopts_same_nonce_and_does_not_repeat_wsl(self):
        self.interrupt = 'windows'
        with self.assertRaises(ci.CIInterrupted):
            self.run_gate()
        saved_path, = Path(self.tmp.name).glob('ci-*.json')
        saved = json.loads(saved_path.read_text())
        self.assertEqual(saved['index'], 1)
        row, = slots.lease_queue(self.conn)
        self.assertEqual((row['platform'], row['pinned']), ('windows', 1))
        self.service = ci.CI(self.tmp.name, self.launcher, poll_s=0, resource_check=resources_go)
        self.service.resume({}, 'checkout', conn=self.conn)
        result = self.run_gate()
        self.assertTrue(result['passed'])
        self.assertEqual([c[:2] for c in self.calls], [('launch', 'wsl'), ('launch', 'windows'), ('adopt', 'windows')])
        self.assertEqual(self.calls[-1][2], self.calls[-2][2])
        self.assertEqual(slots.lease_queue(self.conn), [])
        self.assertFalse(saved_path.exists())

    def test_missing_or_wrong_nonce_terminal_holds_request_without_relaunch(self):
        def launched(spec):
            self.calls.append(('launch', spec['placement'], spec['run_id']))
            handle = self.finish(spec)
            (runner.run_dir(self.tmp.name, spec['run_id']) / runner.STATUS_FILE).unlink()
            return handle
        self.launcher.launch = launched
        with self.assertRaises(ci.CIInterrupted):
            self.run_gate()
        saved_path, = Path(self.tmp.name).glob('ci-*.json')
        spec = json.loads(saved_path.read_text())['context']['spec']
        status_path = runner.run_dir(self.tmp.name, spec['run_id']) / runner.STATUS_FILE
        row, = slots.lease_queue(self.conn)
        self.assertEqual(row['pinned'], 1)
        runner.write_json_atomic(status_path, dict(state='exited', nonce='wrong', exit_code=0))
        with self.assertRaises(ci.CIInterrupted):
            self.run_gate()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(saved_path.exists())
        self.assertEqual(slots.lease_queue(self.conn)[0]['pinned'], 1)
        runner.write_json_atomic(status_path, dict(state='exited', nonce=spec['nonce'], run_id=spec['run_id'], exit_code=0))
        self.launcher.launch = self.launch
        self.assertTrue(self.run_gate()['passed'])
        self.assertEqual([call[0] for call in self.calls], ['launch', 'adopt', 'launch'])

    def test_stop_after_restart_targets_saved_nonce_and_keeps_capacity(self):
        self.interrupt = 'wsl'
        with self.assertRaises(ci.CIInterrupted):
            self.run_gate()
        saved_path, = Path(self.tmp.name).glob('ci-*.json')
        spec = json.loads(saved_path.read_text())['context']['spec']
        restarted = ci.CI(self.tmp.name, self.launcher, resource_check=resources_go)
        with patch.object(runner, 'request_stop', return_value=True) as stop:
            restarted.stop_active(self.conn, DirectPort(self.conn).admission()['epoch'])
        stop.assert_called_once_with(self.tmp.name, spec['run_id'], nonce=spec['nonce'])
        row, = slots.lease_queue(self.conn)
        self.assertEqual(row['pinned'], 1)
        self.assertTrue(saved_path.exists())

    def test_legacy_saved_request_keeps_issued_platform_order(self):
        self.interrupt = 'wsl'
        with self.assertRaises(ci.CIInterrupted):
            self.run_gate()
        saved_path, = Path(self.tmp.name).glob('ci-*.json')
        saved = json.loads(saved_path.read_text())
        saved.pop('order')
        saved['context']['spec']['placement'] = 'windows'  # pre-upgrade first platform
        plan_path = runner.run_dir(self.tmp.name, saved['context']['spec']['run_id']) / 'plan.json'
        plan = json.loads(plan_path.read_text())
        runner.write_json_atomic(plan_path, {**plan, 'platform': 'windows'})
        runner.write_json_atomic(saved_path, saved)
        self.interrupt = None
        result = self.run_gate()
        self.assertTrue(result['passed'])
        self.assertEqual([c[:2] for c in self.calls[1:]], [('adopt', 'windows'), ('launch', 'wsl')])


class PlanContract(unittest.TestCase):
    def test_wsl_plan_uses_python3_without_running_a_script(self):
        from drover import placement, repos
        with tempfile.TemporaryDirectory() as root:
            prog = repos.normalize({'name': 'p', 'repo': root, 'scripts': {'test': '.drover/test.ps1'}})
            service = ci.CI(root, launcher=SimpleNamespace(), resource_check=resources_go)
            with patch.object(placement, 'prepare_worktree'), patch.object(repos, 'load', return_value=prog), \
                    patch.object(ci, 'changed_paths', return_value=[]), \
                    patch.object(ci, 'changed_docs_only', return_value=False), \
                    patch.object(service, '_diff_lines', return_value=0):
                plan = service.prepare(prog, root, 'exact-sha', 'plan', platform='wsl')
            self.assertEqual(plan['spec']['argv'][0], 'python3')
            self.assertEqual(plan['spec']['placement'], 'wsl')
