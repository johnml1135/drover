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
