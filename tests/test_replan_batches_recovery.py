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


class Recovery(BatchPipelineCase):
    def test_crash_before_access_event(self):
        planning = self.planning()
        tids, requests = self.queue()
        batch = self.prepare(planning)
        self.assertEqual(ops.events(self.conn, kind='dispatch.access'), [])
        restored = planning.restore(batch.run_id)
        self.assertEqual(tuple(restored.task_ids), tuple(batch.task_ids))
        self.assertEqual(restored.cwd, batch.cwd)
        self.assertEqual(set(restored.read_roots), set(batch.read_roots))
        # An enrolled process with no launch/access event is still reaped for all members.
        self.fl.scan = lambda want_wsl=False: {}
        from tests.test_pipeline import FakeHandle
        self.fl.adopt = lambda spec, started=None: FakeHandle()
        restarted = self.make(scan_every_s=0)
        restarted.recover_orphans()
        self.assertEqual([len(ops.events(self.conn, task_id=tid, kind='replan-attempt-finished'))
                          for tid in tids], [0, 0])
        restarted.watch_runs()
        for tid in tids:
            event, = ops.events(self.conn, task_id=tid, kind='replan-attempt-finished')
            self.assertEqual(event['data']['outcome'], contract.ORPHANED)
            self.assertEqual(self.get(tid)['replans_used'], 0)

    def test_terminal_files_after_restart(self):
        tids, requests, spec = self.run_batch()
        directory = runner.run_dir(self.runs, spec['run_id'])
        directory.mkdir(parents=True, exist_ok=True)
        text = envelope([(tids[0], single.REVISE), (tids[1], single.OWNER)])
        event = {'type': 'result', 'result': text, 'usage': {'input_tokens': 101, 'output_tokens': 37}}
        (directory / runner.OUT_FILE).write_text(json.dumps(event) + '\n')
        (directory / runner.STATUS_FILE).write_text(json.dumps(
            {'run_id': spec['run_id'], 'state': 'exited', 'exit_code': 0}))
        adopted = []
        def adopt(saved, started=None):
            handle = launch.RunnerHandle(saved, self.runs, 100000, poll_s=0.01)
            adopted.append(handle)
            self.assertIsNotNone(handle.wait(5), 'terminal files must be consumable after restart')
            return handle
        self.fl.adopt = adopt
        self.fl.scan = lambda want_wsl=False: {}
        restarted = self.make(scan_every_s=0)
        restarted.recover_orphans()
        restarted.tick()
        self.assertEqual(len(adopted), 1)
        self.assertEqual(self.get(tids[0])['goal'], single.REVISE['goal'])
        self.assertEqual(self.status(tids[1]), states.NEEDS_OWNER)
        runs = [r for tid in tids for r in ops.list_runs(self.conn, tid) if r['role'] == 'planner']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['tokens_in'], 101)

    def test_two_clean_scans_all_members(self):
        tids, requests, spec = self.run_batch()
        self.fl.scan = lambda want_wsl=False: {}
        restarted = self.make(scan_every_s=0)
        restarted.recover_orphans()
        self.assertTrue(all(not ops.events(self.conn, task_id=tid, kind='replan-attempt-finished') for tid in tids))
        restarted.watch_runs()
        for tid in tids:
            event, = ops.events(self.conn, task_id=tid, kind='replan-attempt-finished')
            self.assertEqual(event['data']['outcome'], contract.ORPHANED)
            self.assertEqual(self.get(tid)['replans_used'], 0)
            self.assertTrue(any(r['kind'] == 'deadline' for r in self.waiting().pending(('task', tid))))

    def test_unknown_scan_releases_none(self):
        tids, requests, spec = self.run_batch()
        def unknown(want_wsl=False):
            raise runner.ScanError('process list unavailable')
        self.fl.scan = unknown
        restarted = self.make(scan_every_s=0)
        for _ in range(3):
            restarted.recover_orphans()
            restarted.watch_runs()
        self.assertTrue(all(not ops.events(self.conn, task_id=tid, kind='replan-attempt-finished') for tid in tids))
        self.assertEqual({r['current_run'] for r in self.waiting().replans()}, {spec['run_id']})
        # An unknown scan between misses resets the count of clean missing scans.
        self.fl.scan = lambda want_wsl=False: {}
        restarted.recover_orphans()
        self.fl.scan = unknown
        restarted.watch_runs()
        self.fl.scan = lambda want_wsl=False: {}
        restarted.watch_runs()
        self.assertTrue(all(not ops.events(self.conn, task_id=tid, kind='replan-attempt-finished') for tid in tids))
        restarted.watch_runs()
        self.assertTrue(all(ops.events(self.conn, task_id=tid, kind='replan-attempt-finished') for tid in tids))

    def test_uncertain_launch_guards_nonanchor_across_restart(self):
        tids, requests = self.queue()
        original = self.fl.launch
        def uncertain(spec):
            original(spec)
            raise launch.LaunchError('runner may still exist', cancel_requested=True)
        self.fl.launch = uncertain
        self.d.tick()
        self.assertEqual(len(self.planners()), 1)
        spec, = self.planners()
        self.fl.launch = original
        self.waiting().expire(self.d.now() + 1800)
        for tid in tids:
            ops.set_status(self.conn, tid, states.READY, actor='owner')
        restarted = self.make(scan_every_s=0)
        restarted.recover_orphans()
        restarted.tick()
        self.assertEqual(self.fl.of('worker'), [])
        self.assertIsNone(ops.claim_next(self.conn, {'actor': 'dispatcher'}))
        for tid in tids:
            with self.subTest(tid=tid), self.assertRaises(ValueError):
                ops.delete_task(self.conn, tid, confirm=True)
        self.fl.scan = lambda want_wsl=False: {}
        restarted.watch_runs()
        cleared, = ops.events(self.conn, kind='launch-cancel-cleared')
        self.assertEqual(cleared['data']['run'], spec['run_id'])
        self.assertIsNotNone(ops.claim_next(self.conn, {'actor': 'dispatcher'}))

    def test_all_requests_done_still_reaps_run(self):
        tids, requests, spec = self.run_batch()
        for tid in tids:
            ops.set_status(self.conn, tid, states.CANCELLED, actor='owner')
        self.assertEqual(self.waiting().replans(), [])
        self.set_result(spec, tids)
        restarted = self.make(scan_every_s=0)
        restarted.recover_orphans()
        restarted.tick()
        runs = [r for tid in tids for r in ops.list_runs(self.conn, tid)]
        self.assertEqual(len(runs), 1)
        self.assertIsNotNone(runs[0]['ended'])
        self.assertEqual([self.status(tid) for tid in tids], [states.CANCELLED] * 2)
        self.assertEqual([self.get(tid)['goal'] for tid in tids], ['Goal 0', 'Goal 1'])
        self.assertEqual(len(ops.events(self.conn, kind='run-finished')), 1)

    def test_repeated_completion_no_duplicate_usage(self):
        planning = self.planning()
        tids, requests = self.queue()
        batch = self.prepare(planning)
        terminal = single.result(contract.NO_RECORD, envelope([(tids[0], single.SPLIT),
                                                               (tids[1], single.REVISE)]))
        first = planning.finish(batch.run_id, terminal, now=1001)
        before = waiting_tests.logical(self.conn)
        # Recreate the facade as after a hub restart; replay comes from durable member events.
        second = self.planning().finish(batch.run_id, terminal, now=1002)
        self.assertEqual(first, second)
        self.assertEqual(waiting_tests.logical(self.conn), before)
        self.assertEqual(len(ops.events(self.conn, kind='run-finished')), 1)
        self.assertEqual(len(ops.events(self.conn, kind='replan-attempt-finished')), 2)


class WaitRelease(BatchLedgerCase):
    """Erratum 4: event resolvers release per-request waits, never a batch wait."""
    def followers(self, count=2):
        w = self.waiting()
        sources = [self.task() for _ in range(count)]
        followers = [self.task() for _ in range(count)]
        for source, follower in zip(sources, followers):
            ops.add_dep(self.conn, follower, source)
        requests = [w.request_replan(tid, 'owner-request') for tid in sources]
        return w, sources, followers, requests

    def test_replan_completed_releases_each_member(self):
        w, tids, followers, requests = self.followers()
        started = self.enroll(w, requests)
        self.complete(w, requests, started)
        for tid in [*tids, *followers]:
            self.assertEqual(w.pending(('task', tid)), [])
            self.assertEqual(self.status(tid), states.READY)
        self.assertEqual(len(ops.events(self.conn, kind='replan-completed')), 2)
        self.assertEqual({row['subject_kind'] for row in w.pending()}, set())

    def test_question_answered_releases_only_its_member(self):
        w, tids, followers, requests = self.followers()
        started = self.enroll(w, requests)
        self.complete(w, requests, started, {tid: single.OWNER for tid in tids})
        questions = {q['target_id']: q['id'] for q in ops.open_questions(self.conn)}
        self.assertEqual(set(questions), set(tids))
        ops.answer_question(self.conn, questions[tids[0]], text='Supported interface')
        self.assertEqual([self.status(tid) for tid in (tids[0], followers[0])], [states.READY] * 2)
        self.assertEqual(self.status(tids[1]), states.NEEDS_OWNER)
        self.assertTrue(w.pending(('task', followers[1])))
        ops.answer_question(self.conn, questions[tids[1]], text='Other interface')
        self.assertEqual(w.pending(), [])

    def test_child_terminal_events_release_split_followers(self):
        w, tids, followers, requests = self.followers()
        started = self.enroll(w, requests)
        self.complete(w, requests, started, {tids[0]: single.SPLIT, tids[1]: single.REVISE})
        child_ids = [d['depends_on'] for d in ops.deps_of(self.conn, followers[0])]
        self.assertEqual(len(child_ids), 2)
        self.assertEqual(self.status(followers[1]), states.READY)
        ops.set_status(self.conn, child_ids[0], states.CANCELLED, actor='owner')
        self.assertEqual(self.status(followers[0]), states.BLOCKED)
        ops.set_status(self.conn, child_ids[1], states.CANCELLED, actor='owner')
        self.assertEqual(self.status(followers[0]), states.READY)
        self.assertEqual(w.pending(('task', followers[0])), [])

    def test_expire_releases_neutral_deadlines_only(self):
        w, tids, requests = self.requests()
        started = self.enroll(w, requests)
        self.complete(w, requests, started, {}, outcome=contract.PROVIDER_ERROR, now=1000)
        w.expire(1059)
        for tid in tids:
            self.assertEqual(len(self.deadline(w, tid)), 1)
        w.expire(1060)
        for tid in tids:
            self.assertEqual(self.deadline(w, tid), [])
            self.assertEqual({r['kind'] for r in w.pending(('task', tid))}, {'replan'})
            self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)
        self.assertIsNotNone(self.enroll(w, requests, now=1060))

    def test_simultaneous_causes_release_independently(self):
        # Change the resolver order: no first release may wake the follower early.
        for order in (('question', 'children', 'deadline', 'manual'),
                      ('manual', 'deadline', 'children', 'question')):
            with self.subTest(order=order):
                w = self.waiting()
                split_source, owner_source, follower = [self.task() for _ in range(3)]
                ops.add_dep(self.conn, follower, split_source)
                ops.add_dep(self.conn, follower, owner_source)
                requests = [w.request_replan(tid, 'owner-request') for tid in (split_source, owner_source)]
                started = self.enroll(w, requests)
                self.complete(w, requests, started, {split_source: single.SPLIT, owner_source: single.OWNER})
                children = [d['depends_on'] for d in ops.deps_of(self.conn, follower)
                            if d['depends_on'] != owner_source]
                self.assertEqual(len(children), 2)
                ops.block(self.conn, follower, 'independent manual cause', actor='owner')
                w.delay(follower, 'planner', 'replan-neutral', 1000, minutes=1)
                self.assertEqual({r['kind'] for r in w.pending(('task', follower))},
                                 {'task-terminal', 'owner-answer', 'manual', 'deadline'})
                question, = [q for q in ops.open_questions(self.conn) if q['target_id'] == owner_source]
                for index, resolver in enumerate(order):
                    if resolver == 'question':
                        ops.answer_question(self.conn, question['id'], text='Proceed')
                    elif resolver == 'children':
                        for child in children:
                            ops.set_status(self.conn, child, states.CANCELLED, actor='owner')
                    elif resolver == 'deadline':
                        w.expire(1060)
                    else:
                        ops.unblock(self.conn, follower, actor='owner')
                    self.assertEqual(self.status(follower), states.READY if index == 3 else states.BLOCKED)
                self.assertEqual(w.pending(('task', follower)), [])


class PlannerPermissions(unittest.TestCase):
    def test_wsl_claude_planner_command_denies_bash(self):
        argv, stdin = launch.build_argv(dict(role='planner', write=False, backend='claude',
            model='opus-5.5', effort='high', cwd='/planner', placement='wsl',
            system_append='read-only policy', user_prompt='inspect only'))
        permissions = json.loads(argv[argv.index('--settings') + 1])['permissions']
        self.assertTrue({'Edit', 'Write', 'Bash'} <= set(permissions['deny']))
        self.assertNotIn('Bash', argv[argv.index('--tools') + 1].split(','))


class BatchRegressions(BatchLedgerCase):
    def test_archived_member_aborts_unlaunched_batch(self):
        w, tids, requests = self.requests()
        started = self.enroll(w, requests)
        ops.archive_task(self.conn, tids[0])
        self.assertFalse(w.verify_replans(started['run_id'], {r['id']: self.inputs() for r in requests}, now=1001))
        self.assertEqual(self.physical(tids)[0]['outcome'], contract.UNVERIFIED)
        self.assertEqual([ops.get_task(self.conn, tid)['replans_used'] for tid in tids], [0, 0])
        self.assertEqual(self.deadline(w, tids[1]), [])
        remaining, = w.replans()
        self.assertEqual((remaining['task_id'], remaining['state']), (tids[1], contract.REPLAN_QUEUED))


class EnrollmentRegressions(BatchPipelineCase):
    def test_owner_edit_before_enrollment_rejects_stale_brief(self):
        planning = self.planning()
        tids, _ = self.queue()
        real = Waiting.start_replans
        def changed(waiting, *args, **kwargs):
            ops.update_task(self.conn, tids[0], goal='Owner edit before enrollment')
            return real(waiting, *args, **kwargs)
        with patch.object(Waiting, 'start_replans', changed):
            self.assertIsNone(self.prepare(planning))
        self.assertEqual([r for tid in tids for r in ops.list_runs(self.conn, tid)], [])
        self.assertEqual(self.get(tids[0])['goal'], 'Owner edit before enrollment')
        retry = self.prepare(planning)
        self.assertIn('Owner edit before enrollment', retry.brief['user_prompt'])


class PlannerInspectionCache(BatchPipelineCase):
    def test_each_candidate_inspected_once_per_tick_across_slots(self):
        from drover.replan import Replanning
        planning = self.planning()
        tids, _ = self.queue(3)
        routes = {tid: {**ROUTE, 'seat': f'seat{i}'} for i, tid in enumerate(tids)}   # three separate batches
        calls = []
        real = Replanning._inputs
        def counting(replanning, task):
            calls.append(task['id'])
            return real(replanning, task)
        inspected = {}
        with patch.object(Replanning, '_inputs', counting):
            batches = [planning.prepare(lambda task, inputs: routes[task['id']], now=1000, inspected=inspected)
                       for _ in range(3)]
        self.assertTrue(all(batches))
        # One inspection per candidate, plus the one post-enrollment freshness check per started member.
        self.assertEqual(sorted(calls), sorted(tids * 2))
        self.assertEqual(set(inspected), set(tids))


class ReadOnlyShellAuthority(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        (Path(self.dir.name) / '.git').mkdir()

    def tools(self, role, **extra):
        argv, _ = launch.build_argv(dict(role=role, write=False, backend='claude', model='opus-5.5', effort='high',
                                         cwd=self.dir.name, placement='wsl', system_append='p', user_prompt='u', **extra))
        settings = json.loads(argv[argv.index('--settings') + 1])
        return argv[argv.index('--tools') + 1].split(','), settings['permissions']['deny']

    def test_only_planner_loses_bash_on_wsl(self):
        for role in ('review', 'investigator', 'researcher'):
            tools, deny = self.tools(role)
            self.assertIn('Bash', tools, role)
            self.assertNotIn('Bash', deny, role)
        tools, deny = self.tools('planner')
        self.assertNotIn('Bash', tools)
        self.assertIn('Bash', deny)
