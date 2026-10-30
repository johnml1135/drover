"""Durable replan requests, bounded records, graph replacement and dispatch."""
import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import brief, config, contract, launch, pipeline, repos
from drover.ledger import connect, db, ops, states
from drover.port import DirectPort
from tests.test_waiting import LedgerCase, logical, require
from tests.test_pipeline import result
from tests.rcw_fixture import PipelineBase
from tests.tiers import integration

from tests.test_replans import ROUTE, REVISE, OWNER, SPLIT, inspected, PlannerDispatchCase, time_now


class Dispatch(PlannerDispatchCase):
    def test_request_to_each_result(self):
        w = self.waiting()
        for record in (REVISE, SPLIT, OWNER):
            tid = self.task(goal='old', acceptance=['old'], scope_files=['src/*'])
            ops.add_comment(self.conn, 'task', tid, 'owner', 'Replan this', replan=True)
            self.d.tick()
            sp = next(sp for sp in self.planners() if sp['task']['id'] == tid)
            self.assertFalse(sp['write'])
            self.fl.handles[sp['run_id']].result = result('no-record', json.dumps(record))
            self.d.tick()
            runs = [r for r in ops.list_runs(self.conn, tid) if r['role'] == 'planner']
            self.assertEqual([r['outcome'] for r in runs], ['done'])
            self.assertEqual(self.status(tid), {'revise': 'ready', 'split': 'cancelled', 'needs-owner': 'needs-owner'}[record['action']])
            self.assertFalse(any(sp['task']['id'] == tid for sp in self.fl.of('worker')))
            if record['action'] == 'revise':
                ops.archive_task(self.conn, tid)
        self.assertTrue(any(r['state'] == 'owner' for r in w.replans()))

    def test_admission_readonly_and_fallback(self):
        w = self.waiting()
        tid = self.task()
        w.request_replan(tid, 'owner-request')
        ops.update_task(self.conn, tid, model_pin={'backend': 'codex', 'model': 'gpt-6-luna'})
        ops.set_setting(self.conn, 'dispatch_enabled', 'false')
        self.d.tick()
        self.assertEqual(self.planners(), [])
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        self.d = self.make(resources=lambda **kw: {'verdict': 'queue', 'slots': 0, 'reasons': []})
        self.d.tick()
        self.assertEqual(self.planners(), [])
        self.d = self.make(allow=lambda backend: False)
        self.d.tick()
        self.assertEqual(self.planners(), [])
        self.d = self.make()
        self.d.tick()
        self.d.tick()
        sp, = self.planners()
        self.assertNotEqual(sp['model'], 'gpt-6-luna')
        self.assertFalse(sp['write'])
        self.assertEqual((sp['wall_s'], sp['idle_s']), (600, 120))
        for backend in ('claude', 'codex'):
            argv, stdin = launch.build_argv({**sp, 'backend': backend, 'write': True})
            joined = ' '.join(argv)
            self.assertNotIn('dangerously-skip-permissions', joined)
            if backend == 'codex':
                self.assertIn('read-only', argv)
            else:
                settings = json.loads(argv[argv.index('--settings') + 1])
                self.assertNotIn('Edit', settings['permissions']['allow'])
                self.assertNotIn('Write', settings['permissions']['allow'])
                self.assertIn('Edit', settings['permissions']['deny'])
                self.assertIn('Write', settings['permissions']['deny'])
        self.fl.handles[sp['run_id']].result = {**result('error', json.dumps(REVISE)), 'provider_error': 'provider down'}
        self.d.tick()
        self.assertEqual(self.get(tid)['replans_used'], 0)
        self.assertEqual([r['outcome'] for r in ops.list_runs(self.conn, tid)], ['provider-error'])
        # The planner uses the existing director fallback chain, including the personal
        # Claude seat before the final Codex seat, independently of a worker pin.
        cfg = config.validate({'seats': [
            {'name': 'claude', 'backend': 'claude', 'director': True, 'placement': 'windows'},
            {'name': 'personal', 'backend': 'claude', 'priority': 2, 'placement': 'windows'},
            {'name': 'codex', 'backend': 'codex', 'priority': 3, 'placement': 'wsl'},
        ]})
        config.use(cfg)
        self.addCleanup(config.reset)
        from drover import route
        heads = {name: {'remaining_pct': 90} for name in ('claude', 'personal', 'codex')}
        for seat in ('claude', 'personal', 'codex'):
            chosen = route.choose({'role': 'planner', 'model_pin': {'backend': 'codex', 'model': 'gpt-6-luna'}},
                                  {}, seats=cfg['seats'], seat_headroom=heads)
            self.assertEqual(chosen['seat'], seat)
            self.assertNotEqual(chosen['model'], 'gpt-6-luna')
            heads[seat] = {'remaining_pct': 0}
        self.assertFalse(route.choose({'role': 'planner'}, {}, seats=cfg['seats'], seat_headroom=heads).get('backend'))
        # Resource slots are shared across requests, rather than counted per task.
        ops.add_policy_version(self.conn, 'planner-cap', {'parallel_cap': 1})
        for _ in range(2):
            candidate = self.task()
            w.request_replan(candidate, 'owner-request')
        cap = self.make(resources=lambda **kw: {'verdict': 'go', 'slots': 1, 'reasons': []})
        previous = len(self.planners())
        cap.tick()
        cap.tick()
        self.assertEqual(len(self.planners()) - previous, 1)


class Recovery(PlannerDispatchCase):
    def test_active_worker_and_restart(self):
        w = self.waiting()
        tid = self.task()
        self.d.tick()
        worker, = self.fl.of('worker')
        request = w.request_replan(tid, 'owner-request')
        self.d.tick()
        self.assertEqual(self.planners(), [])
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.assertEqual(self.fl.of('gate'), [])
        sp, = self.planners()
        restarted = self.make()
        restarted.recover_orphans()
        restarted.tick()
        self.assertEqual(len(self.planners()), 1)
        self.assertEqual(next(r for r in w.replans() if r['id'] == request['id'])['current_run'], sp['run_id'])
        self.assertEqual(len([r for r in ops.list_runs(self.conn, tid) if r['role'] == 'worker']), 1)

    def test_orphan_cancel_and_snapshot_change(self):
        w = self.waiting()
        tid = self.task()
        w.request_replan(tid, 'owner-request')
        self.d.tick()
        sp, = self.planners()
        ops.update_task(self.conn, tid, goal='changed during planning')
        self.fl.handles[sp['run_id']].result = result('no-record', json.dumps(REVISE))
        self.d.tick()
        self.assertEqual(self.get(tid)['goal'], 'changed during planning')
        self.assertEqual(self.get(tid)['replans_used'], 0)
        self.assertEqual(ops.list_runs(self.conn, tid)[-1]['outcome'], 'unverified')
        w.expire(time_now(self.d) + 1800)
        self.d.tick()
        latest = self.planners()[-1]
        # Two clean scans without the planner process make it orphaned, never worker-ready.
        self.fl.scan = lambda want_wsl=False: {}
        restarted = self.make(scan_every_s=0)
        restarted.recover_orphans()
        restarted.watch_runs()
        self.assertEqual(ops.list_runs(self.conn, tid)[-1]['outcome'], 'orphaned')
        self.assertEqual(self.get(tid)['replans_used'], 0)
        self.fl.handles[latest['run_id']].result = result('error')
        self.assertTrue(any(row['kind'] == 'deadline' for row in w.pending(('task', tid))))
        w.expire(time_now(self.d) + 3600)
        self.fl.scan = lambda want_wsl=False: {sp['run_id']: sp['placement'] for sp in self.planners()
                                              if self.fl.handles[sp['run_id']].poll() is None}
        self.d = self.make()
        self.d.tick()
        latest = self.planners()[-1]
        ops.set_status(self.conn, tid, 'cancelled', actor='owner')
        self.fl.handles[latest['run_id']].result = result('no-record', json.dumps(REVISE))
        self.d.tick()
        self.assertEqual(self.status(tid), 'cancelled')
        self.assertEqual(w.replans(), [])
        self.assertEqual(w.pending(('task', tid)), [])


class PlannerCommandExecution(unittest.TestCase):
    def test_composed_planner_argv_runs_with_readonly_permissions(self):
        for backend in ('claude', 'codex'):
            spec = dict(role='planner', write=True, backend=backend,
                        model='gpt-6.1-sol' if backend == 'codex' else 'opus-5.5', effort='high',
                        cwd='/planner', placement='windows', system_append='planner policy', user_prompt='plan only')
            argv, stdin = launch.build_argv(spec)
            self.assertEqual(stdin, 'plan only')
            if backend == 'codex':
                self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
            else:
                permissions = json.loads(argv[argv.index('--settings') + 1])['permissions']
                self.assertTrue({'Edit', 'Write', 'Bash'} <= set(permissions['deny']))
                self.assertFalse({'Edit', 'Write', 'Bash'} & set(permissions['allow']))


class PlannerRegressions(PlannerDispatchCase):
    def test_cancelled_launch_stays_guarded_across_restart(self):
        w = self.waiting()
        tid = self.task()
        w.request_replan(tid, 'owner-request')
        original = self.fl.launch
        def uncertain(spec):
            original(spec)
            raise launch.LaunchError('runner may still exist', cancel_requested=True)
        self.fl.launch = uncertain
        self.d.tick()
        self.fl.launch = original
        w.expire(self.d.now() + 1800)
        restarted = self.make(scan_every_s=0)
        restarted.tick()
        self.assertEqual(len(self.planners()), 1)
        self.fl.scan = lambda want_wsl=False: {}
        restarted.tick()
        self.assertEqual(len(self.planners()), 2)
        latest = self.planners()[-1]
        self.fl.handles[latest['run_id']].result = result('no-record', json.dumps(REVISE))
        restarted.tick()
        w.request_replan(tid, 'owner-request')
        again = self.make(scan_every_s=0)
        again.tick()
        self.assertEqual(len(self.planners()), 3)


class PlannerPredecessors(PlannerDispatchCase):
    def test_worker_error_during_request_is_superseded_without_retry(self):
        tid = self.task()
        self.d.tick()
        worker, = self.fl.of('worker')
        self.waiting().request_replan(tid, 'owner-request')
        self.fl.handles[worker['run_id']].result = result('no-record')
        self.d.tick()
        self.assertEqual(ops.list_runs(self.conn, tid)[0]['outcome'], 'unverified')
        self.assertEqual(ops.events(self.conn, task_id=tid, kind='finish-error'), [])
        self.assertEqual(len(self.planners()), 1)

    def test_started_gate_and_review_finish_before_planner(self):
        for stage in ('gate', 'review'):
            with self.subTest(stage=stage):
                tid = self.task(scope_files=[stage + '.txt'])
                self.d.tick()
                sha = self.worker_commits(tid, stage + '.txt')
                self.run_worker(tid, sha)
                self.d.tick()
                if stage == 'review':
                    self.d.tick()
                spec = next(s for s in reversed(self.fl.of(stage)) if s['task']['id'] == tid)
                completed = self.fl.handles[spec['run_id']].result
                self.fl.handles[spec['run_id']].result = None
                request = self.waiting().request_replan(tid, 'owner-request')
                self.d.tick()
                self.assertFalse(any(s['task']['id'] == tid for s in self.planners()))
                self.fl.handles[spec['run_id']].result = (result('no-record', json.dumps({
                    'status': 'done', 'sha': sha, 'verdict': 'LAND@' + sha, 'findings': [], 'note': 'ok'}))
                    if stage == 'review' else completed)
                self.d.tick()
                planner, = [s for s in self.planners() if s['task']['id'] == tid]
                self.assertEqual(planner['request_ids'], [request['id']])
                self.assertEqual(ops.list_runs(self.conn, tid)[0]['outcome'], 'unverified')
                self.assertTrue(ops.events(self.conn, task_id=tid, kind='replan.stage-finished'))
                self.assertEqual(self.status(tid), 'blocked')
