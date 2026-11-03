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

ROUTE = {**single.ROUTE, 'placement': 'windows', 'harness': 'codex'}
USAGE = {'tokens_in': 101, 'tokens_out': 37, 'tokens_cached': 11,
         'allowance_used': {'seven_day_pct': 0.25}}


def method(case, obj, name):
    value = getattr(obj, name, None)
    case.assertTrue(callable(value), f'{type(obj).__name__}.{name} interface is absent')
    return value


def envelope(pairs):
    return json.dumps({'results': [{'task_id': tid, 'result': record} for tid, record in pairs]})


class BatchLedgerCase(waiting_tests.LedgerCase):
    inputs = single.Replans.inputs

    def requests(self, count=2, **fields):
        w = self.waiting()
        tids = [self.task(goal='Original', acceptance=['old acceptance'], scope_files=['src/*'],
                          **fields) for _ in range(count)]
        return w, tids, [w.request_replan(tid, 'owner-request') for tid in tids]

    def enroll(self, w, requests, *, now=1000):
        return method(self, w, 'start_replans')(
            [{'request_id': r['id'], 'inputs': self.inputs()} for r in requests], ROUTE, now=now)

    def complete(self, w, requests, started, records=None, *, outcome=contract.DONE,
                 fresh=None, now=1001):
        if records is None:
            records = {r['task_id']: copy.deepcopy(single.REVISE) for r in requests}
        if fresh is None:
            fresh = {r['id']: self.inputs() for r in requests}
        return method(self, w, 'finish_replans')(started['run_id'], outcome, records, USAGE,
                                                fresh=fresh, now=now)

    def physical(self, tids):
        return [run for tid in tids for run in ops.list_runs(self.conn, tid) if run['role'] == 'planner']

    def deadline(self, w, tid):
        return [r for r in w.pending(('task', tid)) if r['kind'] == 'deadline']

    def attempts(self, tid):
        return ops.events(self.conn, task_id=tid, kind='replan-attempt-finished')


class BatchPipelineCase(single.PlannerDispatchCase):
    def setUp(self):
        super().setUp()
        config.use(config.validate({'paths': {'state_dir': str(Path(self.tmp.name) / 'state')}}))
        self.addCleanup(config.reset)

    def queue(self, count=2, **fields):
        tids = [self.task(goal=f'Goal {i}', acceptance=[f'Acceptance {i}'],
                          scope_files=[f'part{i}/*'], **fields) for i in range(count)]
        return tids, [self.waiting().request_replan(tid, 'owner-request') for tid in tids]

    def planning(self):
        return waiting_tests.require(self, 'drover.replan', 'Replanning')(self.pipe)

    def prepare(self, planning, assign=None, now=1000):
        return planning.prepare(assign or (lambda task, inputs: dict(ROUTE)), now=now)

    def run_batch(self, count=2):
        tids, requests = self.queue(count)
        self.d.tick()
        planners = self.planners()
        self.assertEqual(len(planners), 1, 'queued requests must share one physical planner')
        spec = planners[0]
        self.assertEqual({r['current_run'] for r in self.waiting().replans()}, {spec['run_id']})
        return tids, requests, spec

    def set_result(self, spec, tids, records=None):
        records = records or [single.REVISE] * len(tids)
        self.fl.handles[spec['run_id']].result = single.result(
            contract.NO_RECORD, envelope(list(zip(tids, records))))


class CherryPick(unittest.TestCase):
    """The batch tip's replay, at the one process fake: a timed-out cherry-pick is not a rebase conflict."""
    BASE, BEFORE, FORK, COMMIT = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 40

    def replay(self, pick):
        import tempfile
        from drover import ci, landing_batch, ownership
        from tests.fakeci import git_spawns
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        work = Path(root.name) / 'batch'
        work.mkdir()
        (work / '.git').write_text('gitdir: ../repo/.git/worktrees/batch\n', encoding='utf-8')     # relative: no repair
        self.calls, self.timeouts = [], set()

        def reply(repo, *args, timeout):
            self.calls.append(args)
            self.timeouts.add(timeout)
            if args[0] == 'cherry-pick' and args[1] != '--abort':
                return pick()
            out = {'rev-parse': self.BEFORE, 'merge-base': self.FORK, 'rev-list': self.COMMIT}.get(args[0], '')
            return subprocess.CompletedProcess(args, 0, out + '\n' if out else '', '')
        git_spawns(self, reply)
        with patch.object(ownership, 'tag'):
            return landing_batch.build_tip(Path(root.name) / 'repo', work, self.BASE,
                                           [{'id': 't1', 'branch': 'feat', 'head': self.COMMIT}])

    def after_pick(self):
        picked = self.calls.index(('cherry-pick', '--allow-empty', self.COMMIT))
        return self.calls[picked + 1:]

    def test_cherry_pick_timeout_raises_ci_error_and_neither_drops_the_task_nor_resets(self):
        from drover import ci

        def stuck():
            raise subprocess.TimeoutExpired(['git', 'cherry-pick'], 600.0)
        with self.assertRaisesRegex(ci.CIError, 'cherry-pick'):
            self.replay(stuck)       # raised, so no conflict list names t1
        self.assertEqual(self.after_pick(), [])     # no abort, and no reset over a lock the timed-out pick may hold
        self.assertEqual(self.timeouts, {600.0})    # section 5.1: the replay's reads and the pick had no timeout: LONG

    def test_a_cherry_pick_that_cannot_start_raises_ci_error_and_aborts_nothing(self):
        from drover import ci

        def missing():
            raise FileNotFoundError(2, 'No such file or directory')
        with self.assertRaisesRegex(ci.CIError, r'cherry-pick .*: unavailable, not a conflict'):
            self.replay(missing)
        self.assertEqual(self.after_pick(), [])

    def test_formation_reads_run_at_long(self):
        from drover import landing_batch
        from tests.fakeci import git_spawns
        timeouts = []
        git_spawns(self, lambda repo, *args, timeout: timeouts.append(timeout) or subprocess.CompletedProcess(args, 0, 'x\n', ''))
        self.assertEqual((landing_batch.branch_head('/repo', 'feat'), landing_batch.task_files('/repo', 'main', 'feat')), ('x', ['x']))
        self.assertEqual(timeouts, [600.0, 600.0])

    def test_cherry_pick_conflict_is_still_a_conflict(self):
        tip, applied, conflicts = self.replay(lambda: subprocess.CompletedProcess([], 1, '', 'CONFLICT (content)'))
        self.assertEqual((applied, conflicts), ([], ['t1']))
        self.assertEqual(self.after_pick()[:2], [('cherry-pick', '--abort'), ('reset', '-q', '--hard', self.BEFORE)])
