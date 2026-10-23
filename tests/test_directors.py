"""Supervision through the public runtime seam; no paid backends are used."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from drover.directors import DirectorRuntime, RunnerDirectorAdapter
from drover.ledger import actions, ops
from drover.ledger.db import connect
from drover.notifications import Notifications
from drover.port import DirectPort
from drover import usage


class Handle:
    def __init__(self):
        self.result = None

    def poll(self):
        return self.result


class ControlledAdapter:
    capabilities = dict(live_offer=False, verified_resume=False, fresh_start=True,
                        enforced_token_cap=True, authenticated_processing=True, secure_handoff=True)

    def __init__(self, service):
        self.service = service
        self.started, self.stops, self.adopted = [], [], []
        self.states, self.handles = {}, {}
        self.before_launch = lambda: None
        self.fail_launch = False
        self.process = False

    def launch(self, spec, credential, guard):
        self.before_launch()
        guard()
        # This executor makes zero model calls, hence enforces the reserved cap.
        identity = self.service.authenticate(credential)
        assert identity['generation'] == spec['generation']
        assert spec['max_tokens'] > 0
        self.started.append(dict(spec))
        self.states[spec['nonce']] = 'alive'
        self.handles[spec['run_id']] = Handle()
        if self.process:
            lease = self.service.acquire(identity['role_id'], identity['session_id'])
            messages = self.service.receive(identity['role_id'])
            self.service.commit(identity['role_id'], lease['lease_token'], {'processed': True},
                                [m['delivery_id'] for m in messages], [], 'fixture-process', release=True,
                                delivery_versions={m['delivery_id']: m['message_version'] for m in messages})
            self.handles[spec['run_id']].result = {'status': 'done', 'usage': {'tokens_in': 0, 'tokens_out': 0}}
            self.states[spec['nonce']] = 'gone'
        if self.fail_launch:
            raise OSError('lost launch response')
        return self.handles[spec['run_id']]

    def probe(self, spec):
        return self.states.get(spec['nonce'], 'unknown')

    def adopt(self, spec):
        self.adopted.append(spec['run_id'])
        return self.handles[spec['run_id']]

    def stop(self, spec):
        self.stops.append((spec['run_id'], spec['nonce']))
        return True  # A request is not proof that the process has ceased.


class DirectorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.conn = connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.time = 1000.
        self.service = Notifications(self.conn, now=lambda: self.time)
        self.port = DirectPort(self.conn, now=lambda: self.time)
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        ops.set_setting(self.conn, 'director_execution_enabled', 'true')
        ops.set_setting(self.conn, 'director_execution', json.dumps({'director:a': {
            'backend': 'claude', 'model': 'test-model', 'seat': 'fixture',
            'max_tokens': 20, 'placement': 'windows', 'cwd': self.tmp.name}}))
        self.service.register('director:a', allowed_models=['test-model'], token_budget=100,
                              wake_limit_per_hour=4, replacement_limit=1)
        self.adapter = ControlledAdapter(self.service)
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        self.capacity = {'slots': 2, 'admit': lambda spec: None}

    def tick(self):
        return self.runtime.tick(self.service.admission(), self.capacity)

    def wake(self):
        task = ops.create_task(self.conn, 'Child', 'repo', kind='research', initiating_role='director:a')
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'report'}, task_id=task['id'])
        return task

    def test_dormant_roles_have_no_seat_session_or_budget(self):
        self.tick()
        self.assertEqual(self.runtime.active_runs(), [])
        self.assertEqual(self.service.role_state('director:a')['tokens_reserved'], 0)
        self.assertIsNone(self.service.role_state('director:a')['session_id'])

    def observer_failure(self, starting=False):
        from drover import runner
        from drover.launch import RunnerHandle
        self.wake()
        rid, = self.tick()['launched']
        spec = self.adapter.started[0]
        path = runner.run_dir(self.tmp.name, rid) / runner.STATUS_FILE
        path.parent.mkdir(parents=True)
        runner.write_json_atomic(path, dict(nonce=spec['nonce'], state='starting' if starting else 'running'))
        handle = RunnerHandle(spec, self.tmp.name, None, clock=lambda: self.time, autostart=False)
        self.addCleanup(handle.cancel)
        if starting:
            self.time += handle.start_timeout_s + 1
            handle._run()
        else:
            with patch.object(handle, '_ingest', side_effect=OSError('observer failed')):
                handle._run()
        self.assertEqual(handle.poll()['status'], 'error')
        launcher = SimpleNamespace(runs_dir=self.tmp.name, scan=lambda _: {rid: {}},
            adopt=lambda _: handle, cancel=lambda rid, nonce: runner.request_stop(self.tmp.name, rid, nonce=nonce))
        adapter = RunnerDirectorAdapter(launcher)
        adapter.capabilities = self.adapter.capabilities
        self.runtime.adapter = adapter
        self.runtime.live[rid] = handle
        session = self.service.role_state('director:a')['session_id']
        result = self.tick()
        self.assertEqual(result['launched'], [])
        self.assertEqual([r['run_id'] for r in self.runtime.active_runs()], [rid])
        self.assertEqual(self.service.role_state('director:a')['session_id'], session)
        self.assertEqual(self.service.role_state('director:a')['replacements_used'], 0)
        self.assertEqual(usage.active_load(self.conn)['fixture'], 1)
        self.assertIn('observation', str(result['recovery']['held']))
        stopped = self.service.stop('owner', 'observer-stop')
        self.tick()
        self.service.resume('owner', stopped['epoch'], 'observer-resume')
        path.unlink()
        self.runtime = DirectorRuntime(self.port, adapter, now=lambda: self.time)
        self.tick()
        self.assertEqual([r['run_id'] for r in self.runtime.active_runs()], [rid])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM action_outbox WHERE kind='director.stop'").fetchone()[0], 1)
        runner.write_json_atomic(path, dict(nonce=spec['nonce'], state='killed'))
        self.runtime.recover()
        self.assertEqual(self.runtime.active_runs(), [])

    def test_runner_observation_error_does_not_authorize_replacement(self):
        self.observer_failure()

    def test_runner_starting_timeout_does_not_authorize_replacement(self):
        self.observer_failure(starting=True)

    def two_roles(self):
        self.service.register('director:b', allowed_models=['test-model'], token_budget=100)
        configs = json.loads(self.port.get_setting('director_execution'))
        configs['director:b'] = configs['director:a']
        ops.set_setting(self.conn, 'director_execution', json.dumps(configs))
        self.service.broadcast('owner', ['director:a', 'director:b'], 'work', urgency='urgent')

    def uncertain_capacity(self, callback=False):
        self.two_roles()
        self.adapter.fail_launch = True
        calls = []
        def slots():
            calls.append(len(self.runtime.active_runs()))
            return 1 - len(self.runtime.active_runs())
        self.capacity['slots'] = slots if callback else 1
        result = self.tick()
        self.assertEqual(len(self.adapter.started), 1)
        self.assertEqual(len(self.runtime.active_runs()), 1)
        self.assertEqual(result['launched'], [])
        self.assertEqual(result['capacity_used'], 1)
        self.assertIn('capacity-unavailable', str(result['held']))
        if callback:
            self.assertIn(1, calls)

    def test_unknown_launch_consumes_static_capacity(self):
        self.uncertain_capacity()

    def test_unknown_launch_rechecks_durable_capacity(self):
        self.uncertain_capacity(callback=True)

    def test_pre_effect_refusal_releases_capacity_for_other_role(self):
        from drover.launch import AdmissionHeld
        self.two_roles()
        launch = self.adapter.launch
        def refuse_first(spec, credential, guard):
            if spec['role_id'] == 'director:a':
                raise AdmissionHeld('fixture-pre-effect-refusal')
            return launch(spec, credential, guard)
        self.adapter.launch = refuse_first
        self.capacity['slots'] = 1
        result = self.tick()
        self.assertEqual([r['role_id'] for r in self.runtime.active_runs()], ['director:b'])
        self.assertEqual(len(self.adapter.started), 1)
        self.assertEqual(result['capacity_used'], 1)

    def test_runner_probe_requires_nonce_and_terminal_evidence_when_supervisor_is_missing(self):
        from drover import runner
        nonce = 'a' * 32
        spec = dict(run_id=1, nonce=nonce, placement='windows')
        launcher = SimpleNamespace(runs_dir=self.tmp.name, scan=lambda want_wsl: {})
        adapter = RunnerDirectorAdapter(launcher)
        path = runner.run_dir(self.tmp.name, 1) / runner.STATUS_FILE
        path.parent.mkdir(exist_ok=True)
        runner.write_json_atomic(path, {'nonce': nonce, 'state': 'running'})
        self.assertEqual(adapter.probe(spec), 'unknown')
        runner.write_json_atomic(path, {'nonce': nonce, 'state': 'exited'})
        self.assertEqual(adapter.probe(spec), 'gone')
        runner.write_json_atomic(path, {'nonce': 'b' * 32, 'state': 'exited'})
        self.assertEqual(adapter.probe(spec), 'unknown')

    def test_authenticated_adapter_processing_finishes_without_ack_or_credential_fabrication(self):
        self.wake()
        self.adapter.process = True
        rid, = self.tick()['launched']
        self.tick()
        self.assertEqual(len(self.adapter.started), 1)
        state = self.service.role_state('director:a')
        self.assertEqual(state['checkpoint'], {'processed': True})
        self.assertFalse(state['pending_wake'])
        self.assertEqual(self.runtime.active_runs(), [])
        self.assertEqual(self.conn.execute('SELECT tokens_out FROM runs WHERE id=?', (rid,)).fetchone()[0], 0)
        self.assertEqual(state['tokens_reserved'], 20)
        self.assertNotIn('credential', str(self.adapter.started))
        self.assertNotIn('credential', str(ops.events(self.conn)))
        self.assertNotIn('lease_token', str(ops.events(self.conn)))

    def test_capability_gaps_are_held_without_issuing_session(self):
        self.wake()
        for capability in ('enforced_token_cap', 'authenticated_processing', 'secure_handoff', 'fresh_start'):
            self.adapter.capabilities = {**ControlledAdapter.capabilities, capability: False}
            self.assertIn(capability.replace('_', '-'), str(self.tick()))
        self.assertEqual(self.adapter.started, [])
        self.assertEqual(self.service.role_state('director:a')['tokens_reserved'], 0)
        self.assertIsNone(self.service.role_state('director:a')['session_id'])

    def test_actor_control_stops_only_affected_runtime_and_lease_expiry_never_duplicates_live_run(self):
        self.wake()
        rid, = self.tick()['launched']
        self.time += 61
        self.tick()
        self.assertEqual(len(self.adapter.started), 1)
        self.service.register('director:other')
        self.service.require_skill('owner', 'review', 'v2', ['director:other'], 'unrelated')
        self.tick()
        self.assertEqual(self.adapter.stops, [])
        self.service.require_skill('owner', 'review', 'v3', ['director:a'], 'affected')
        self.tick()
        self.assertEqual(self.adapter.stops, [(rid, self.adapter.started[0]['nonce'])])
        self.assertEqual(len(self.adapter.started), 1)

    def test_durable_run_seat_counts_after_restart_and_uncertain_stop(self):
        self.wake()
        self.tick()
        self.assertEqual(usage.active_load(self.conn)['fixture'], 1)
        self.service.stop('owner', 'stop')
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        self.tick()
        self.assertEqual(usage.active_load(self.conn)['fixture'], 1)
        self.adapter.states[self.adapter.started[0]['nonce']] = 'gone'
        self.tick()
        self.assertNotIn('fixture', usage.active_load(self.conn))

    def test_coalesced_wakes_use_archived_accounting_without_worker_effects(self):
        child = self.wake()
        out = self.tick()
        run_id, = out['launched']
        self.wake()
        self.tick()
        self.assertEqual(len(self.adapter.started), 1)
        run = self.conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        anchor = ops.get_task(self.conn, run['task_id'])
        self.assertTrue(anchor['archived'])
        self.assertTrue(anchor['internal'])
        self.assertEqual(anchor['attempts'], 0)
        self.assertEqual(ops.get_task(self.conn, child['id'])['attempts'], 0)
        self.assertEqual(ops.events(self.conn, kind='run-started'), [])
        self.assertFalse(any(e['task_id'] == anchor['id'] for e in ops.events(self.conn, kind='task-created')))
        self.assertFalse(self.conn.execute("SELECT 1 FROM notification_intents WHERE json_extract(envelope,'$.task_id')=?", (anchor['id'],)).fetchone())
        self.assertEqual(self.service.role_state('director:a')['tokens_reserved'], 20)

    def test_disabled_and_unsupported_do_not_issue_credentials_or_spend(self):
        self.wake()
        ops.set_setting(self.conn, 'director_execution_enabled', 'false')
        self.assertIn('execution-disabled', str(self.tick()))
        ops.set_setting(self.conn, 'director_execution_enabled', 'true')
        self.runtime = DirectorRuntime(self.port)
        self.assertIn('unsupported', str(self.tick()))
        self.assertIsNone(self.service.role_state('director:a')['session_id'])
        self.assertEqual(self.service.role_state('director:a')['tokens_reserved'], 0)
        self.assertGreater(len(self.service.receive('director:a')), 0)

    def test_capacity_and_allowance_refusal_preserve_wake_and_budget(self):
        self.wake()
        for capacity, reason in (({'slots': 0, 'admit': lambda spec: None}, 'capacity'),
                                 ({'slots': 1, 'admit': lambda spec: 'allowance'}, 'allowance'),
                                 ({'slots': 1, 'admit': lambda spec: 'resources'}, 'resources')):
            self.capacity = capacity
            self.assertIn(reason, str(self.tick()))
        state = self.service.role_state('director:a')
        self.assertTrue(state['pending_wake'])
        self.assertEqual(state['tokens_reserved'], 0)
        self.assertIsNone(state['session_id'])

    def test_unknown_liveness_holds_and_one_verified_replacement_keeps_children(self):
        child = self.wake()
        ops.approve(self.conn, child['id'])
        ops.claim_next(self.conn)
        worker = ops.start_run(self.conn, child['id'], 'worker', 'claude')
        self.tick()
        first = self.adapter.started[0]
        self.adapter.states[first['nonce']] = 'unknown'
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        self.assertIn('uncertain', str(self.tick()))
        self.assertEqual(len(self.adapter.started), 1)
        self.adapter.states[first['nonce']] = 'gone'
        self.tick()
        self.assertEqual(len(self.adapter.started), 2)
        self.assertEqual(self.adapter.started[1]['generation'], first['generation'] + 1)
        self.assertEqual(ops.get_task(self.conn, child['id'])['status'], 'running')
        self.assertIsNone(ops.list_runs(self.conn, child['id'])[0]['ended'])
        self.assertEqual(ops.list_runs(self.conn, child['id'])[0]['id'], worker)
        self.assertEqual(ops.get_task(self.conn, child['id'])['attempts'], 1)
        second = self.adapter.started[1]
        self.adapter.states[second['nonce']] = 'gone'
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        self.assertIn('replacement-budget-exhausted', str(self.tick()))
        self.assertEqual(len(self.adapter.started), 2)
        self.wake()
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        self.assertIn('replacement-budget-exhausted', str(self.tick()))
        self.assertEqual(len(self.adapter.started), 2)

    def test_verified_resume_is_selected_before_fresh_replacement(self):
        self.wake()
        self.tick()
        original = self.adapter.started[0]
        self.adapter.states[original['nonce']] = 'gone'
        self.adapter.capabilities = {**ControlledAdapter.capabilities, 'verified_resume': True, 'fresh_start': False}
        self.adapter.verify_resume = lambda spec: spec['nonce'] == original['nonce']
        self.tick()
        resumed = self.adapter.started[1]
        self.assertEqual(resumed['mode'], 'resume')
        self.assertEqual(resumed['resume_from']['nonce'], original['nonce'])
        self.assertEqual(resumed['generation'], original['generation'] + 1)
        self.assertEqual(self.service.role_state('director:a')['replacements_used'], 1)

    def test_restart_adopts_by_nonce_before_retry_and_does_not_rotate_session(self):
        self.wake()
        self.adapter.fail_launch = True
        self.tick()
        state = self.service.role_state('director:a')
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        self.tick()
        self.assertEqual(len(self.adapter.started), 1)
        self.assertEqual(self.adapter.adopted, [self.adapter.started[0]['run_id']])
        self.assertEqual(self.service.role_state('director:a')['generation'], state['generation'])
        self.assertEqual(len(self.runtime.active_runs()), 1)

    def test_stale_finish_cannot_release_new_session_or_change_checkpoint(self):
        self.wake()
        run_id, = self.tick()['launched']
        new = self.service.issue_session('director:a', 'owner-replacement')
        lease = self.service.acquire('director:a', new['session_id'])
        self.service.commit('director:a', lease['lease_token'], {'continuation': True}, [], [], 'new')
        self.runtime.finish(run_id, {'status': 'done', 'usage': {}})
        state = self.service.role_state('director:a')
        self.assertEqual(state['lease']['generation'], new['generation'])
        self.assertEqual(state['checkpoint'], {'continuation': True})
        self.assertTrue(state['pending_wake'])

    def test_normal_lifecycle_rejects_director_impersonation(self):
        child = self.wake()
        with self.assertRaises(PermissionError):
            ops.start_run(self.conn, child['id'], 'director', 'claude')
        run_id, = self.tick()['launched']
        anchor = self.conn.execute('SELECT task_id FROM runs WHERE id=?', (run_id,)).fetchone()[0]
        with self.assertRaises(PermissionError):
            ops.start_run(self.conn, anchor, 'worker', 'claude')
        with self.assertRaises(PermissionError):
            ops.finish_run(self.conn, run_id, 'done')
        with self.assertRaises(PermissionError):
            ops.archive_task(self.conn, anchor, False)
        with self.assertRaises(PermissionError):
            ops.set_status(self.conn, anchor, 'ready')
        self.runtime.finish(run_id, {'status': 'done', 'usage': {'tokens_in': 2, 'tokens_out': 1}})
        self.assertEqual(ops.get_task(self.conn, child['id'])['attempts'], 0)
        self.assertEqual(ops.events(self.conn, kind='run-finished'), [])

    def test_stop_between_admission_and_external_launch_is_fenced(self):
        self.wake()
        self.adapter.before_launch = lambda: self.service.stop('owner', 'race')
        self.tick()
        self.assertEqual(self.adapter.started, [])
        self.assertIn('stop-work', self.service.role_state('director:a')['held_reason'])

    def test_stop_resume_during_capacity_check_cannot_refresh_accepted_epoch(self):
        self.wake()
        def admit(spec):
            stop = self.service.stop('owner', 'capacity-stop')
            self.service.resume('owner', stop['epoch'], 'capacity-resume')
        self.capacity['admit'] = admit
        self.assertIn('control-epoch-changed', str(self.tick()))
        self.assertEqual(self.adapter.started, [])
        self.assertIsNone(self.service.role_state('director:a')['session_id'])
        self.assertEqual(self.service.role_state('director:a')['tokens_reserved'], 0)

    def test_stop_requests_nonce_and_reports_uncertain_termination_after_restart(self):
        self.wake()
        run_id, = self.tick()['launched']
        stop = self.service.stop('owner', 'stop')
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        result = self.tick()
        self.assertEqual(self.adapter.stops, [(run_id, self.adapter.started[0]['nonce'])])
        self.assertIn('termination-uncertain', str(result))
        self.tick()
        self.assertEqual(len(self.adapter.stops), 1)
        self.assertEqual(len(self.runtime.active_runs()), 1)
        self.adapter.states[self.adapter.started[0]['nonce']] = 'gone'
        self.tick()
        self.assertEqual(self.runtime.active_runs(), [])
        self.assertFalse(self.service.admission()['allowed'])
        self.assertEqual(self.service.admission()['epoch'], stop['epoch'])

    def test_internal_launch_outbox_is_not_a_role_action_or_local_task_effect(self):
        self.wake()
        self.tick()
        row = self.conn.execute("SELECT * FROM action_outbox WHERE kind='director.launch'").fetchone()
        self.assertIsNotNone(row)
        before = dict(row)
        actions.execute(self.conn, row['action_id'])
        self.assertEqual(dict(self.conn.execute('SELECT * FROM action_outbox WHERE action_id=?', (row['action_id'],)).fetchone()), before)
        with self.assertRaises(ValueError):
            self.service.register('bad', action_allowlist=['director.launch'])
