"""T1 contract tests for one physical planner serving independent requests.

Seams approved by replan/plan.md: Waiting's batch operations and read accessors,
contract.parse_replan_records, Replanning.prepare/finish/restore, and Dispatcher.
Absent interfaces are assertions so this module is collectable on the T1 base.
SQL below is restricted to migration fixtures/constraints and fault injection.
"""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from drover import brief, config, contract, launch, runner
from drover.ledger import connect, db, ops, schema, states
from drover.ledger.waiting import Waiting, cause_key, REPLAN, OWNER_ANSWER
from tests.tiers import integration
from tests import test_replans as single
from tests import test_waiting as waiting_tests

from tests.test_replan_batches import ROUTE, USAGE, method, envelope, BatchLedgerCase, BatchPipelineCase


class Preparation(BatchPipelineCase):
    def test_repo_assignment_partition(self):
        planning = self.planning()
        tids, requests = self.queue(9)
        ops.update_task(self.conn, tids[8], repo='P')
        variants = [dict(ROUTE), dict(ROUTE), {**ROUTE, 'seat': 'other'},
                    {**ROUTE, 'model': 'gpt-6-luna'}, {**ROUTE, 'effort': 'low'},
                    {**ROUTE, 'harness': 'other'}, {**ROUTE, 'placement': 'wsl'},
                    {**ROUTE, 'backend': 'claude'}, dict(ROUTE)]
        routes = dict(zip(tids, variants))
        first = self.prepare(planning, lambda task, inputs: routes[task['id']])
        self.assertEqual(set(first.task_ids), {tids[0], tids[1], tids[8]})
        second = self.prepare(planning, lambda task, inputs: routes[task['id']])
        self.assertEqual(tuple(second.task_ids), (tids[2],))
        # Different configured repos with identical assignments still cannot mix.
        other = self.new_repo()
        self.addCleanup(other.cleanup)
        self.pipe.all_repos()['q'] = single.repos.normalize({**self.prog, 'name': 'q',
                                                          'repo': str(other.repo)})
        q = self.task(repo='q')
        self.waiting().request_replan(q, 'owner-request')
        # Deny the remaining p assignments so q is independently eligible.
        third = self.prepare(planning, lambda task, inputs: ROUTE if task['repo'] == 'q' else None)
        self.assertEqual(tuple(third.task_ids), (q,))

    def test_epic_dependency_scope_preference_topology(self):
        planning = self.planning()
        epic = ops.create_epic(self.conn, 'p', 'preferred')['id']
        other = ops.create_epic(self.conn, 'p', 'unrelated')['id']
        seed = self.task(epic_id=epic, scope_files=['shared/*'])
        unrelated = [self.task(epic_id=other, scope_files=[f'u{i}/*']) for i in range(8)]
        sibling = self.task(epic_id=epic, scope_files=['different/*'])
        prerequisite = self.task(scope_files=['outside/*'])
        overlap = self.task(epic_id=epic, scope_files=['shared/a'])
        ops.add_dep(self.conn, seed, prerequisite)
        # Earliest request is seed even though its prerequisite must render first.
        for tid in [seed, *unrelated, sibling, prerequisite, overlap]:
            self.waiting().request_replan(tid, 'owner-request')
        batch = self.prepare(planning)
        self.assertEqual(len(batch.task_ids), 8)
        self.assertTrue({seed, sibling, prerequisite, overlap} <= set(batch.task_ids))
        self.assertLess(batch.task_ids.index(prerequisite), batch.task_ids.index(seed))
        prompt = batch.brief['user_prompt']
        self.assertLess(prompt.index(prerequisite), prompt.index(seed))
        self.assertEqual(len(set(batch.task_ids) & set(unrelated)), 4)

    def test_eight_and_brief_caps(self):
        planning = self.planning()
        tids, requests = self.queue(10)
        batch = self.prepare(planning)
        self.assertEqual(len(batch.task_ids), 8)
        self.assertLessEqual(batch.brief['tokens'], 15000)
        self.assertLessEqual(brief.estimate_tokens(batch.brief['system_append'] + batch.brief['user_prompt']), 15000)
        for tid in batch.task_ids:
            task = self.get(tid)
            for mandatory in (tid, task['goal'], *task['acceptance'], *task['scope_files']):
                self.assertIn(mandatory, batch.brief['user_prompt'])
        remaining = self.prepare(planning)
        self.assertEqual(set(remaining.task_ids), set(tids) - set(batch.task_ids))
        # Mandatory content forces membership reduction, rather than truncation.
        more, _ = self.queue(3)
        for tid in more:
            ops.update_task(self.conn, tid, goal='required goal ' * 1500)
        reduced = self.prepare(planning)
        self.assertLess(len(reduced.task_ids), 3)
        self.assertGreater(len(reduced.task_ids), 0)
        for tid in reduced.task_ids:
            self.assertIn(self.get(tid)['goal'], reduced.brief['user_prompt'])
        self.assertLessEqual(reduced.brief['tokens'], 15000)

    def test_oversized_seed_does_not_starve(self):
        planning = self.planning()
        tids, _ = self.queue(3)
        ops.update_task(self.conn, tids[0], goal='mandatory ' * 100000)
        # A bad source branch also cannot block a later valid request.
        ops.update_task(self.conn, tids[1], branch='missing-source-branch')
        batch = self.prepare(planning)
        self.assertEqual(tuple(batch.task_ids), (tids[2],))
        for tid in tids[:2]:
            self.assertEqual(self.get(tid)['replans_used'], 0)
            self.assertTrue(any(r['kind'] == 'deadline' for r in self.waiting().pending(('task', tid))))
            self.assertTrue(ops.events(self.conn, task_id=tid, kind='replan-deferred'))

    def test_postclaim_change_aborts_without_sibling_backoff(self):
        planning = self.planning()
        tids, requests = self.queue()
        real = method(self, Waiting, 'start_replans')
        def enroll_then_change(waiting, *args, **kwargs):
            attached = real(waiting, *args, **kwargs)
            if attached:
                ops.update_task(self.conn, tids[0], goal='owner changed after enrollment')
            return attached
        with patch.object(Waiting, 'start_replans', enroll_then_change):
            self.assertIsNone(self.prepare(planning))
        physical = [r for tid in tids for r in ops.list_runs(self.conn, tid)]
        self.assertEqual(len(physical), 1)
        self.assertEqual(physical[0]['outcome'], contract.UNVERIFIED)
        self.assertTrue(any(r['kind'] == 'deadline' for r in self.waiting().pending(('task', tids[0]))))
        self.assertFalse(any(r['kind'] == 'deadline' for r in self.waiting().pending(('task', tids[1]))))
        self.assertEqual([self.get(tid)['replans_used'] for tid in tids], [0, 0])
        self.assertEqual(self.planners(), [])
        retry = self.prepare(planning, now=1002)
        self.assertEqual(tuple(retry.task_ids), (tids[1],))

    def test_shared_read_failure_is_local(self):
        planning = self.planning()
        tids, requests = self.queue(3)
        # Two members use one immutable topic snapshot; the third uses main.
        self.fix.branch('context', {'context.txt': 'immutable input'})
        for tid in tids[:2]:
            ops.update_task(self.conn, tid, branch='context', read_access=['p@context'])
        batch = self.prepare(planning)
        self.assertEqual(set(batch.task_ids), set(tids))
        # restore must expose the same roots and must not create writable worktrees.
        restored = planning.restore(batch.run_id)
        self.assertEqual(set(restored.read_roots), set(batch.read_roots))
        topic = [Path(root) for root in batch.read_roots if (Path(root) / 'context.txt').exists()]
        self.assertEqual(len(topic), 1, 'shared immutable context should be deduplicated')
        (topic[0] / 'context.txt').write_text('changed read input')
        terminal = single.result(contract.NO_RECORD, envelope([(tid, single.REVISE) for tid in tids]))
        planning.finish(batch.run_id, terminal, now=1001)
        self.assertEqual([self.get(tid)['goal'] for tid in tids[:2]], ['Goal 0', 'Goal 1'])
        self.assertEqual(self.get(tids[2])['goal'], single.REVISE['goal'])
        self.assertEqual([self.get(tid)['replans_used'] for tid in tids], [0, 0, 1])


class Dispatch(BatchPipelineCase):
    def test_eight_requests_one_process_one_seat(self):
        ops.add_policy_version(self.conn, 'batch-cap', {'parallel_cap': 1})
        tids, requests, spec = self.run_batch(8)
        self.d.tick()
        self.assertEqual(len(self.planners()), 1)
        self.assertEqual([self.get(tid)['attempts'] for tid in tids], [1] * 8)
        self.assertEqual(len(self.d.live), 1)
        # A second process is capacity-blocked, despite another eligible request.
        ninth, _ = self.queue(1)
        self.d.tick()
        self.assertEqual(len(self.planners()), 1)
        self.assertIsNone(next(r for r in self.waiting().replans() if r['task_id'] == ninth[0])['current_run'])
        self.set_result(spec, tids)
        self.d.tick()
        self.assertEqual([self.get(tid)['replans_used'] for tid in tids], [1] * 8)
        self.assertTrue(all(self.get(tid)['goal'] == single.REVISE['goal'] for tid in tids))

    def test_other_bucket_can_launch(self):
        other = self.new_repo()
        self.addCleanup(other.cleanup)
        self.pipe.all_repos()['q'] = single.repos.normalize({**self.prog, 'name': 'q', 'repo': str(other.repo)})
        p, _ = self.queue(2)
        q, _ = self.queue(2, repo='q')
        self.d.tick()
        self.assertEqual(len(self.planners()), 2)
        attachments = {r['task_id']: r['current_run'] for r in self.waiting().replans()}
        self.assertEqual(attachments[p[0]], attachments[p[1]])
        self.assertEqual(attachments[q[0]], attachments[q[1]])
        self.assertNotEqual(attachments[p[0]], attachments[q[0]])

    def test_every_member_readonly(self):
        tids, requests = self.queue(3)
        sources = [self.pipe.worktree(self.get(tid)) for tid in tids]
        self.d.tick()
        self.assertEqual(len(self.planners()), 1)
        spec, = self.planners()
        self.assertFalse(spec['write'])
        self.assertTrue(set(sources) <= set(spec['read_dirs']) | {spec['cwd']})
        for tid in tids:
            self.assertIn(tid, spec['user_prompt'])
        for backend in ('codex', 'claude'):
            argv, _ = launch.build_argv({**spec, 'backend': backend})
            if backend == 'codex':
                self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
                self.assertNotIn('--add-dir', argv)
            else:
                permissions = json.loads(argv[argv.index('--settings') + 1])['permissions']
                self.assertTrue({'Edit', 'Write', 'Bash'} <= set(permissions['deny']))
                self.assertFalse({'Edit', 'Write', 'Bash'} & set(permissions['allow']))

    @unittest.skipIf(os.name == 'nt', 'executable stand-ins run on WSL; reviewer checks Windows')
    @integration
    def test_composed_batch_command(self):
        # T4 and T5 gated with T6 for this real composed-command test (erratum 2).
        planning = self.planning()
        executable = Path(self.tmp.name) / 'planner-backend'
        executable.write_text(f'#!{sys.executable}\n' + '''import json,os,sys
payload=json.loads(os.environ['BATCH_REPLY'])
with open(os.environ['BATCH_OBSERVED'],'w') as out:
    json.dump({'argv':sys.argv[1:],'stdin':sys.stdin.read()},out)
print(json.dumps(payload))
''')
        executable.chmod(0o755)
        for backend in ('codex', 'claude'):
            tids, requests = self.queue()
            source = self.pipe.worktree(self.get(tids[1]))
            before = self.fix.git('status', '--porcelain', cwd=source)
            batch = self.prepare(planning, lambda task, inputs: {**ROUTE, 'backend': backend})
            self.assertEqual(set(batch.task_ids), set(tids))
            spec = dict(role='planner', backend=backend, binary=str(executable), write=False,
                        placement='windows', model='gpt-6.1-sol' if backend == 'codex' else 'opus-5.5',
                        effort='high', cwd=batch.cwd, read_dirs=list(batch.read_roots), **batch.brief)
            argv, stdin = launch.build_argv(spec)
            observed_file = Path(self.tmp.name) / f'{backend}-observed.json'
            records = [(tids[0], single.REVISE), (tids[1], single.OWNER)]
            ran = subprocess.run(argv, input=stdin, text=True, capture_output=True, check=True,
                                 cwd=batch.cwd, timeout=30,
                                 env={**os.environ, 'BATCH_REPLY': envelope(records),
                                      'BATCH_OBSERVED': str(observed_file)})
            observed = json.loads(observed_file.read_text())
            self.assertTrue(all(tid in observed['stdin'] for tid in tids))
            self.assertIn(str(source), observed['stdin'])
            if backend == 'codex':
                self.assertEqual(observed['argv'][observed['argv'].index('-s') + 1], 'read-only')
                self.assertNotIn('--add-dir', observed['argv'])
            else:
                settings = json.loads(observed['argv'][observed['argv'].index('--settings') + 1])
                self.assertTrue({'Edit', 'Write', 'Bash'} <= set(settings['permissions']['deny']))
            planning.finish(batch.run_id, single.result(contract.NO_RECORD, ran.stdout), now=1001)
            self.assertEqual(self.get(tids[0])['goal'], single.REVISE['goal'])
            self.assertEqual(self.status(tids[1]), states.NEEDS_OWNER)
            self.assertEqual(self.fix.git('status', '--porcelain', cwd=source), before)


class PreparationRegressions(BatchPipelineCase):
    def test_exhausted_seed_does_not_starve_other_members(self):
        planning = self.planning()
        tids, _ = self.queue(3)
        ops.set_setting(self.conn, 'replan_limit', '1')
        first = self.prepare(planning, lambda task, inputs: ROUTE if task['id'] == tids[0] else None)
        planning.finish(first.run_id, single.result(contract.NO_RECORD, '{invalid'), now=1001)
        remaining = self.prepare(planning, now=1002)
        self.assertIsNotNone(remaining)
        self.assertEqual(set(remaining.task_ids), set(tids[1:]))
        self.assertEqual(self.status(tids[0]), states.NEEDS_OWNER)


class EnvelopeRegressions(BatchPipelineCase):
    def test_valid_empty_envelope_retries_members_but_finishes_execution(self):
        planning = self.planning()
        tids, _ = self.queue()
        batch = self.prepare(planning)
        planning.finish(batch.run_id, single.result(contract.NO_RECORD, '{"results":[]}'), now=1001)
        run = ops.list_runs(self.conn, batch.task['id'])[0]
        self.assertEqual(run['outcome'], contract.DONE)
        self.assertEqual([self.get(tid)['replans_used'] for tid in tids], [0, 0])
        self.assertTrue(all(any(w['kind'] == 'deadline' for w in self.waiting().pending(('task', tid))) for tid in tids))


class AssignmentRegressions(BatchPipelineCase):
    def test_restore_keeps_assignment_before_access_event(self):
        planning = self.planning()
        self.queue()
        assignment = {**ROUTE, 'seat': 'other', 'harness': 'other', 'placement': 'wsl'}
        batch = self.prepare(planning, lambda task, inputs: assignment)
        restored = planning.restore(batch.run_id)
        self.assertEqual(restored.assignment, batch.assignment)
        self.assertEqual(ops.events(self.conn, kind='dispatch.access'), [])
