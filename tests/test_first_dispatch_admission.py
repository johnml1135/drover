"""First dispatch admission, infrastructure-failure and pause regressions."""
import json
import unittest
from unittest.mock import patch

from drover import allowance, config, dispatcher, launch, route
from tests.test_dispatcher import Base, res


class Admission(Base):
    def report_for(self, five_hour=8, weekly=90):
        from drover import usage
        cfg = config.validate({'seats': [{'name': 'fake', 'backend': 'claude', 'director': True,
                                         'reserve_pct': 10}]})
        config.use(cfg)
        self.addCleanup(config.reset)
        now = self.d.now()
        usage.manual(self.conn, 'fake', 'five_hour', 100 - five_hour, now + 360, now=now)
        usage.manual(self.conn, 'fake', 'seven_day', 100 - weekly, now + 86400, now=now)
        return cfg['seats'][0], usage.headroom(self.conn, 'fake', now)

    def test_report_agrees_with_routing_near_five_hour_reset(self):
        seat, h = self.report_for()
        self.assertTrue(route._seat_admits(seat, h, protected=True))
        self.assertEqual(route.choose({'role': 'worker', 'size': 'M'}, {}, seats=[seat],
                                      seat_headroom={'fake': h})['seat'], 'fake')
        self.d._admission_report()
        self.assertFalse(json.loads(self.port.get_setting('admission_hold'))['held'])
        self.assertFalse(self.events('admission-hold'))
        self.assertEqual(len(self.events('admission-released')), 1)

    def test_report_names_the_actual_failed_floor(self):
        self.report_for(five_hour=5)
        self.d._admission_report()
        self.assertIn('five-hour floor', json.dumps(self.events('admission-hold')[-1]['data']))
        self.report_for(weekly=9)
        self.d._admission_report()
        self.assertIn('seven-day reserve', json.dumps(self.events('admission-hold')[-1]['data']))

    def test_five_hour_floor_changing_with_time_does_not_duplicate_hold(self):
        self.report_for(five_hour=5)
        self.d._admission_report()
        now = self.d.now()
        self.d.now = lambda: now + 60
        self.d._admission_report()
        self.assertEqual(len(self.events('admission-hold')), 1)

    def test_report_checks_capacity_backend_health_and_missing_readings(self):
        seat, _ = self.report_for()
        seat['max_workers'] = 1
        with patch.object(self.d, '_seat_load', return_value={'fake': 1}):
            self.d._admission_report()
        self.assertIn('max_workers', json.dumps(self.events('admission-hold')[-1]['data']))
        with patch.object(self.d, '_is_unavailable', return_value=True):
            self.d._admission_report()
        self.assertIn('backend unavailable', json.dumps(self.events('admission-hold')[-1]['data']))
        self.port.set_setting('seat_health', json.dumps({'fake': 'missing home'}))
        self.d._admission_report()
        self.assertIn('seat health error', json.dumps(self.events('admission-hold')[-1]['data']))
        self.port.set_setting('seat_health', '{}')
        self.conn.execute('DELETE FROM allowance_readings')
        self.d._admission_report()
        self.assertIn('no current allowance reading', json.dumps(self.events('admission-hold')[-1]['data']))

    def test_seat_order_changes_do_not_duplicate_the_same_admission_hold(self):
        from drover import usage
        cfg = config.validate({})
        cfg['seats'].append({**cfg['seats'][1], 'name': 'codex-spare'})
        config.use(cfg)
        self.addCleanup(config.reset)
        self.task()
        self.allow = {'claude': False, 'codex': False}
        self.d.tick()
        rows = usage.order(self.conn, cfg['seats'], now=self.d.now())
        # Polling/order changes do not change any seat's actual admission reason.
        with patch.object(usage, 'order', return_value=list(reversed(rows))):
            self.d._admission_report()
        self.assertEqual(len(self.events('admission-hold')), 1)

    def test_allowance_hold_is_durable_deduplicated_and_released(self):
        self.task()
        self.allow = {'claude': False, 'codex': False}
        self.d.tick()
        self.d.tick()
        events = self.events('admission-hold')
        self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0]['data']['backends']), {'claude', 'codex'})
        self.assertIn('reserve', json.dumps(events[0]['data']))
        self.assertTrue(json.loads(self.port.get_setting('admission_hold'))['held'])
        self.d = self.make()  # a hub restart retains deduplication
        self.d.tick()
        self.assertEqual(len(self.events('admission-hold')), 1)
        self.allow['codex'] = True
        self.d.tick()
        self.assertFalse(json.loads(self.port.get_setting('admission_hold'))['held'])

    def test_host_verification_with_completed_work_goes_to_checks(self):
        tid = self.task()
        self.d.tick()
        record = {'status': 'blocked', 'blocker': {'kind': 'host-verification', 'detail': 'Windows Python unavailable'},
                  'note': 'Implementation complete'}
        self.pipe.commit_ready = lambda *a: 'abc1234'
        self.finish(0, res('blocked', json.dumps(record)))
        self.d.tick()
        self.assertFalse(self.port.waiting.replans())
        self.assertEqual(len(self.events('env-block-ready')), 1)
        self.assertEqual(dispatcher.failures(self.port.list_runs(tid)), 0)

    def test_early_infrastructure_failure_pauses_without_task_failure(self):
        tid = self.task()
        self.d.tick()
        self.finish(0, {**res('error'), 'infrastructure_error': 'CODEX_HOME path does not exist'})
        self.d.tick()
        self.assertEqual(self.port.get_setting('dispatch_enabled'), 'false')
        self.assertEqual(dispatcher.failures(self.port.list_runs(tid)), 0)
        self.assertEqual(self.status(tid), 'ready')
        self.assertEqual(len(self.fl.specs), 1)

    def test_synchronous_setup_failure_stops_further_launches_in_the_tick(self):
        tid = self.task()
        self.task(title='next task')
        with patch.object(self.fl, 'launch', side_effect=launch.LaunchError('spawn-failed: missing executable')) as start:
            self.assertTrue(self.d.tick()['paused'])
        self.assertEqual(start.call_count, 1)
        self.assertEqual(dispatcher.failures(self.port.list_runs(tid)), 0)
        self.assertEqual(self.status(tid), 'ready')

    def assert_missing_executable_pauses(self, error):
        tid = self.task()
        self.task(title='another task')
        with patch.object(self.fl, 'launch', side_effect=error) as start:
            self.assertTrue(self.d.tick()['paused'])
        self.assertEqual(start.call_count, 1)
        self.assertEqual(dispatcher.failures(self.port.list_runs(tid)), 0)
        self.assertEqual(len(self.events('infrastructure-error')), 1)

    def test_missing_executable_pauses_without_launching_another_task(self):
        self.assert_missing_executable_pauses(FileNotFoundError('codex'))

    def test_wrapped_missing_executable_pauses_without_launching_another_task(self):
        self.assert_missing_executable_pauses(launch.LaunchError('cannot start codex: not found on PATH', missing=True))

    def test_review_setup_failure_can_retry_after_dispatch_resumes(self):
        from drover.ledger import ops
        tid = self.task()
        self.d.tick()
        worker = self.port.list_runs(tid)[0]['id']
        self.d.live.pop(worker)
        self.finish(0, res('done'))
        sha = 'a' * 40
        self.pipe.current_head = lambda: sha
        self.port.append_event('worker-done', {'run': worker, 'sha': sha}, tid)
        self.port.finish_run(worker, 'gated')
        self.port.set_status(tid, 'gated')
        run = self.port.start_run(tid, 'review', 'codex', 'gpt-6.1-sol', 'high')
        result = {**res('error'), 'infrastructure_error': 'CODEX_HOME path does not exist'}
        self.d._finish(run, tid, {'role': 'review', 'backend': 'codex'}, result)
        runs = self.port.list_runs(tid)
        self.assertEqual(runs[-1]['outcome'], 'launch-error')
        self.assertEqual(self.status(tid), 'gated')
        self.assertEqual(dispatcher.failures(runs), 0)
        self.assertTrue(self.d._awaiting_review(runs))
        self.d.tick()
        self.assertEqual(len(self.fl.specs), 1)
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        self.assertEqual(self.d.tick()['reviewed'], [tid])
        self.assertEqual(self.fl.specs[-1]['role'], 'review')
        self.assertEqual(self.fl.specs[-1]['sha'], sha)

    def test_gate_setup_failure_does_not_pause_model_dispatch(self):
        tid = self.task()
        run = self.port.start_run(tid, 'gate', 'local', 'gate', 'high')
        self.d._launch_error(self.port.get_task(tid), run, {'backend': 'local'}, 'wsl',
                             launch.LaunchError('spawn-failed: test executable missing', missing=True), role='gate')
        self.assertEqual(self.port.get_setting('dispatch_enabled'), 'true')
        self.assertEqual(self.events('infrastructure-error'), [])

    def test_paused_dispatch_does_not_prepare_replans_or_sweep_provider_failures(self):
        from drover.ledger import ops
        for n in range(12):
            tid = self.task(title=f'blocked {n}')
            self.port.waiting.request_replan(tid, 'owner-request')
        ops.set_setting(self.conn, 'dispatch_enabled', 'false')
        with patch.object(self.d.replanning, 'prepare', side_effect=AssertionError('paused')):
            self.assertTrue(self.d.tick()['paused'])
