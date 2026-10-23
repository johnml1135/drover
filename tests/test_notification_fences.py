"""STOP barriers at ordinary dispatch and landing public boundaries."""
import tempfile
import unittest
from unittest.mock import patch, Mock

from drover import ci, landing
from drover.launch import AdmissionHeld
from drover.launch import RunnerLauncher
from drover.notifications import Notifications
from drover.ledger import ops
from tests.test_dispatcher import Base, res, DONE
from tests.test_landing import LandingCase
from tests.test_replans import PlannerDispatchCase


class WorkerFences(Base):
    def test_stop_then_resume_between_ticks_still_stops_previously_admitted_run(self):
        self.task()
        self.d.tick()
        calls = []
        self.fl.cancel = lambda rid, nonce: calls.append((rid, nonce)) or True
        service = Notifications(self.conn)
        stop = service.stop('owner', 'stop')
        service.resume('owner', stop['epoch'], 'resume')
        self.d.tick()
        self.assertEqual(calls, [(self.fl.specs[0]['run_id'], self.fl.specs[0]['nonce'])])

    def test_stop_after_gate_preparation_fences_gate_launcher(self):
        tid = self.task()
        self.d.tick()
        self.finish(0, res('done', DONE))
        prepare = self.pipe.ci.prepare

        def prepare_stop(*a, **kw):
            plan = prepare(*a, **kw)
            Notifications(self.conn).stop('owner', 'gate-prepared')
            return plan

        with patch.object(self.pipe.ci, 'prepare', side_effect=prepare_stop), \
                patch.object(self.fl, 'launch', wraps=self.fl.launch) as launch:
            self.d.tick()
        self.assertFalse(launch.called)
        self.assertNotEqual(self.status(tid), 'gated')

    def test_restart_open_director_counts_against_parallel_cap_before_worker_claim(self):
        from drover import config
        from drover.directors import DirectorRuntime
        from tests.test_directors import ControlledAdapter
        import json
        service = Notifications(self.conn)
        seat = config.director_seats()[0]
        service.register('director:a', allowed_models=['fixture'], token_budget=20)
        service.broadcast('owner', ['director:a'], 'work', urgency='urgent')
        ops.set_setting(self.conn, 'director_execution_enabled', 'true')
        ops.set_setting(self.conn, 'parallel_cap', '1')
        ops.set_setting(self.conn, 'director_execution', json.dumps({'director:a': dict(
            backend=seat['backend'], model='fixture', seat=seat['name'], max_tokens=20,
            placement=seat['placement'], cwd=self.tmp.name)}))
        adapter = ControlledAdapter(service)
        self.d.directors = DirectorRuntime(self.port, adapter)
        worker = self.task()
        self.d.tick()
        self.assertEqual(len(adapter.started), 1)
        self.assertEqual(self.fl.specs, [])
        self.d = self.make()
        self.d.directors = DirectorRuntime(self.port, adapter)
        self.d.tick()
        self.assertEqual(len(adapter.started), 1)
        self.assertEqual(self.status(worker), 'ready')
        self.assertEqual(self.d._seat_load()[seat['name']], 1)

    def test_stop_blocks_all_claimers_and_survives_new_dispatcher(self):
        tid = self.task()
        Notifications(self.conn).stop('owner', 'stop')
        self.assertIsNone(ops.claim_next(self.conn))
        self.d = self.make()
        self.assertEqual(self.d.tick()['launched'], [])
        self.assertEqual(self.status(tid), 'ready')

    def test_stop_during_worktree_preparation_blocks_actual_launch(self):
        tid = self.task()
        original = self.pipe.worktree

        def prepare(*a, **kw):
            result = original(*a, **kw)
            Notifications(self.conn).stop('owner', 'between-admission-launch')
            return result

        with patch.object(self.pipe, 'worktree', side_effect=prepare):
            self.d.tick()
        self.assertEqual(self.fl.specs, [])
        self.assertNotEqual(self.status(tid), 'running')

    def test_stop_requests_running_nonce_without_claiming_cessation(self):
        self.task()
        self.d.tick()
        spec = self.fl.specs[0]
        calls = []
        self.fl.cancel = lambda rid, nonce: calls.append((rid, nonce)) or True
        Notifications(self.conn).stop('owner', 'stop')
        self.d.tick()
        self.assertEqual(calls, [(spec['run_id'], spec['nonce'])])
        self.assertIn(spec['run_id'], self.d.live)
        self.assertTrue(ops.events(self.conn, kind='run.stop-held'))


class LandingFences(LandingCase):
    def test_running_group_ci_stop_persists_nonce_and_unknown_termination_for_restart(self):
        import json
        from drover import runner
        self.task('feature', {'good.txt': 'good'})
        launched = []
        service = Notifications(self.conn)

        def launch(spec):
            launched.append(spec)
            handle = Mock()
            def poll():
                service.stop('owner', 'ci-running')
                return None
            handle.poll = poll
            return handle

        with patch.object(self.ci.launcher, 'launch', side_effect=launch):
            with self.assertRaises(ci.CIInterrupted):
                self.ci.run(self.prog, self.r.repo, self.r.git('rev-parse', 'feature'), conn=self.conn)
        spec, = launched
        stop = runner.run_dir(self.ci.runs_dir, spec['run_id']) / runner.STOP_FILE
        self.assertEqual(json.loads(stop.read_text())['nonce'], spec['nonce'])
        restarted = ci.CI(self.ci.runs_dir)
        restarted.stop_active(self.conn, service.admission()['epoch'])
        row = self.conn.execute("SELECT state,result FROM action_outbox WHERE kind='run.stop'").fetchone()
        self.assertEqual(row['state'], 'complete')
        self.assertEqual(json.loads(row['result'])['termination'], 'unknown')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM action_outbox WHERE kind='run.stop'").fetchone()[0], 1)

    def test_ci_stop_after_preparation_prevents_script_launch(self):
        self.task('feature', {'good.txt': 'good'})
        prepare = self.ci.prepare

        def prepare_stop(*a, **kw):
            result = prepare(*a, **kw)
            Notifications(self.conn).stop('owner', 'ci-prepared')
            return result

        with patch.object(self.ci, 'prepare', side_effect=prepare_stop), \
                patch.object(self.ci.launcher, 'launch') as launch:
            with self.assertRaises(AdmissionHeld):
                self.ci.run(self.prog, self.r.repo, self.r.git('rev-parse', 'feature'), conn=self.conn)
        self.assertFalse(launch.called)

    def test_stop_between_group_ci_and_merge_preserves_branch_and_reviewed_task(self):
        tid = self.task('feature', {'good.txt': 'good'})
        old = self.r.git('rev-parse', 'main')
        gate = self.gate

        def gate_stop(*a):
            receipt = gate(*a)
            Notifications(self.conn).stop('owner', 'during-ci')
            return receipt

        self.gate = gate_stop
        result = self.land(push=False)
        self.assertEqual(result['outcome'], 'held')
        self.assertEqual(self.r.git('rev-parse', 'main'), old)
        self.assertEqual(self.status(tid), 'reviewed')

    def test_stop_after_local_merge_records_real_landing_but_fences_push(self):
        tid = self.task('feature', {'good.txt': 'good'})
        merge = landing.local_fast_forward

        def merge_stop(*a, **kw):
            result = merge(*a, **kw)
            Notifications(self.conn).stop('owner', 'after-local-merge')
            return result

        with patch.object(landing, 'local_fast_forward', side_effect=merge_stop), \
                patch.object(landing.origin, 'push_main') as push:
            result = self.land(push=True)
        self.assertEqual(result['outcome'], 'landed')
        self.assertEqual(self.status(tid), 'landed')
        self.assertFalse(push.called)
        self.assertIn('stop-work', result['alert'])


class PlannerFences(PlannerDispatchCase):
    def test_stop_after_planner_enrollment_fences_launch(self):
        tid = self.task()
        self.waiting().request_replan(tid, 'owner-request')
        prepare = self.d.replanning.prepare

        def prepare_stop(*a, **kw):
            batch = prepare(*a, **kw)
            Notifications(self.conn).stop('owner', 'planner-enrolled')
            return batch

        with patch.object(self.d.replanning, 'prepare', side_effect=prepare_stop):
            self.d.tick()
        self.assertEqual(self.planners(), [])


class RunnerBoundary(unittest.TestCase):
    def test_final_detach_boundary_rechecks_control_after_command_preparation(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as runs:
            runner = RunnerLauncher(runs_dir=runs, run=Mock())
            spec = dict(run_id=1, nonce='a' * 32, backend='local', model='gate', argv=['controlled-fixture'],
                        placement='windows', cwd=runs, user_prompt='', write=False)
            def refuse():
                raise AdmissionHeld('stop-work')
            spec['_admission_check'] = refuse
            with patch.object(runner, 'command', return_value=(['controlled-fixture'], {})):
                with self.assertRaises(AdmissionHeld):
                    runner.launch(spec)
            runner.run.assert_not_called()
