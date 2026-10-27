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


class Singleton(unittest.TestCase):
    def test_flat_actions_and_malformed_accounting_contract(self):
        # A passing characterization; batch parsing must preserve this flat format.
        for record in (single.REVISE, single.SPLIT, single.OWNER):
            self.assertEqual(contract.parse_replan_record('observations\n' + json.dumps(record)), record)
        self.assertIsNone(contract.parse_replan_record(envelope([('T-a', single.REVISE)])))
        self.assertIsNone(contract.parse_replan_record('{"action":"revise"'))


class Migration(waiting_tests.LedgerCase):
    def old(self, version):
        conn = connect(Path(self.tmp.name) / f'v{version}.db', migrate_db=False)
        self.addCleanup(conn.close)
        db.migrate(conn, [m for m in schema.MIGRATIONS if m[0] <= version])
        return conn

    def seed(self, conn, tid):
        conn.execute('INSERT INTO tasks(id,repo,title,rank,status,created,updated,approved_at) '
                     'VALUES(?,?,?,?,?,?,?,?)',
                     (tid, 'p', 'old work', 1, states.BLOCKED, '2026-10-20T12:00:00Z',
                      '2026-10-20T12:00:00Z', '2026-10-20T12:00:00Z'))
        return conn.execute('INSERT INTO task_events(kind,actor,data,task_id,at) VALUES(?,?,?,?,?)',
                            ('replan-request', 'owner', '{"trigger":"owner-request"}', tid,
                             '2026-10-20T12:00:00Z')).lastrowid

    def test_v5_v8_preserves_requests(self):
        conn = self.old(5)
        event = self.seed(conn, 'T-legacy')
        before = dict(conn.execute('SELECT * FROM tasks').fetchone())
        db.migrate(conn)
        self.assertGreaterEqual(db.schema_version(conn), 8, 'membership migration V8 is absent')
        request, = Waiting(conn, 'owner').replans()
        self.assertEqual((request['task_id'], request['requesting_event']), ('T-legacy', event))
        after = ops.get_task(conn, 'T-legacy')
        self.assertEqual((after['title'], after['approved_at']), (before['title'], before['approved_at']))
        pending = Waiting(conn, 'owner').pending(('task', 'T-legacy'))
        replan, = [row for row in pending if row['kind'] == REPLAN]
        self.assertEqual(replan['kind'], REPLAN)
        diagnostic, = [row for row in pending if row['kind'] == OWNER_ANSWER]
        self.assertIsNotNone(diagnostic['cause'])
        self.assertTrue(ops.events(conn, kind='waiting.migration-diagnostic'))
        self.assertEqual(conn.execute('SELECT count(*) FROM replan_run_requests').fetchone()[0], 0)
        rows = waiting_tests.logical(conn)
        db.migrate(conn)
        self.assertEqual(waiting_tests.logical(conn), rows)

    def test_v7_open_and_uncleared_cancel_backfill(self):
        conn = self.old(7)
        expected = []
        for index, mode in enumerate(('running', 'done-open', 'cancel-uncleared')):
            tid = f'T-old-{index}'
            event = self.seed(conn, tid)
            run = conn.execute('INSERT INTO runs(task_id,role,backend,model,started,ended,outcome) '
                               'VALUES(?,?,?,?,?,?,?)',
                               (tid, 'planner', 'codex', 'gpt-6.1-sol', '2026-10-20T12:00:00Z',
                                '2026-10-20T12:01:00Z' if mode == 'cancel-uncleared' else None,
                                contract.LAUNCH_ERROR if mode == 'cancel-uncleared' else None)).lastrowid
            request = conn.execute('INSERT INTO replan_requests(task_id,requesting_event,state,current_run,snapshot) '
                                   'VALUES(?,?,?,?,?)',
                                   (tid, event, 'done' if mode == 'done-open' else
                                    'queued' if mode == 'cancel-uncleared' else 'running',
                                    None if mode == 'cancel-uncleared' else run, '{}')).lastrowid
            ops.event(conn, 'dispatch.access', 'dispatcher', {'run': run, 'request_id': request}, task_id=tid)
            finished = None
            if mode == 'cancel-uncleared':
                ops.event(conn, 'launch-cancel', 'dispatcher', {'run': run, 'placement': 'windows'}, task_id=tid)
                finished = ops.event(conn, 'replan-attempt-finished', 'dispatcher',
                                     {'request': request, 'run': run, 'outcome': contract.LAUNCH_ERROR,
                                      'result': {'disposition': 'neutral', 'children': [], 'released': []}}, task_id=tid)
            expected.append((run, request, 0, finished))
        requests_before = [tuple(r) for r in conn.execute('SELECT * FROM replan_requests ORDER BY id')]
        waits_before = [tuple(r) for r in conn.execute('SELECT * FROM waits ORDER BY id')]
        db.migrate(conn)
        self.assertGreaterEqual(db.schema_version(conn), 8, 'membership migration V8 is absent')
        self.assertEqual([tuple(r) for r in conn.execute(
            'SELECT run_id,request_id,position,finished_event FROM replan_run_requests ORDER BY run_id')], expected)
        self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM replan_requests ORDER BY id')], requests_before)
        self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM waits ORDER BY id')], waits_before)
        self.assertEqual(len(method(self, Waiting(conn, 'owner'), 'replan_runs')()), 3)

    def test_v8_atomic_replay(self):
        conn = self.old(7)
        before = waiting_tests.logical(conn)
        conn.execute("CREATE TRIGGER refuse_v8 BEFORE INSERT ON schema_version WHEN new.version=8 "
                     "BEGIN SELECT RAISE(ABORT,'migration fault'); END")
        # On the base there is no V8 to attempt; fail an assertion, not an SQL lookup.
        self.assertTrue(any(version == 8 for version, _ in schema.MIGRATIONS), 'V8 migration is absent')
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'migration fault'):
            db.migrate(conn)
        self.assertEqual(waiting_tests.logical(conn), before)
        self.assertEqual(db.schema_version(conn), 7)
        conn.execute('DROP TRIGGER refuse_v8')
        db.migrate(conn)
        migrated = waiting_tests.logical(conn)
        db.migrate(conn)
        self.assertEqual(waiting_tests.logical(conn), migrated)
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(replan_run_requests)')}
        self.assertEqual(columns, {'run_id', 'request_id', 'position', 'finished_event'})
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='replan_run_requests'").fetchone()[0]
        self.assertIn('CHECK', ddl.upper())
        foreign = list(conn.execute('PRAGMA foreign_key_list(replan_run_requests)'))
        self.assertEqual({r['table'] for r in foreign}, {'runs', 'replan_requests', 'task_events'})
        self.assertTrue(any(r['on_delete'] == 'CASCADE' for r in foreign))


class Protocol(unittest.TestCase):
    def parse(self, text, tids):
        parsed = waiting_tests.require(self, 'drover.contract', 'parse_replan_records')(text, tids)
        return parsed or {}

    def test_missing_invalid_duplicate_unknown_isolation(self):
        pairs = [('T-a', single.REVISE), ('T-b', {'action': 'delete'}),
                 ('T-c', single.REVISE), ('T-c', single.OWNER), ('T-stranger', single.SPLIT)]
        parsed = self.parse(envelope(pairs), ['T-a', 'T-b', 'T-c', 'T-d'])
        self.assertEqual(parsed.get('T-a'), single.REVISE)
        for tid in ('T-b', 'T-c', 'T-d', 'T-stranger'):
            self.assertIsNone(parsed.get(tid), tid)
        # A malformed entry has no authority to invalidate its valid sibling.
        text = json.dumps({'results': [{'task_id': 'T-a', 'result': single.SPLIT},
                                       {'task_id': 'T-b', 'result': {**single.REVISE, 'scope_files': ['../escape']}}]})
        self.assertEqual(self.parse(text, ['T-a', 'T-b']).get('T-a'), single.SPLIT)
        self.assertIsNone(self.parse(text, ['T-a', 'T-b']).get('T-b'))

    def test_truncated_envelope_fails_closed(self):
        valid = envelope([('T-a', single.REVISE), ('T-b', single.OWNER)])
        for text in (valid[:-1], valid[:-2], valid + ' trailing', valid + valid,
                     '{"results":[' + json.dumps(single.REVISE),
                     envelope([('T-a', {**single.REVISE, 'goal': 'é' * 12000}),
                               ('T-b', {**single.REVISE, 'goal': 'é' * 12000})])):
            with self.subTest(text=text[:80]):
                parsed = self.parse(text, ['T-a', 'T-b'])
                self.assertFalse(any(parsed.get(tid) for tid in ('T-a', 'T-b')))
        self.assertEqual(self.parse('notes\n' + valid, ['T-a', 'T-b'])['T-a'], single.REVISE)

    def test_singleton_flat_contract_unchanged(self):
        for record in (single.REVISE, single.SPLIT, single.OWNER):
            self.assertEqual(self.parse(json.dumps(record), ['T-a']), {'T-a': record})
        self.assertFalse(any(self.parse('{bad', ['T-a']).values()))
        self.assertFalse(any(self.parse(json.dumps(single.REVISE), ['T-a', 'T-b']).values()))


class Ledger(BatchLedgerCase):
    def test_one_run_many_requests(self):
        w, tids, requests = self.requests(3)
        started = self.enroll(w, requests)
        self.assertIsNotNone(started)
        self.assertEqual(len(self.physical(tids)), 1)
        self.assertEqual(self.physical(tids)[0]['task_id'], tids[0])
        self.assertEqual({r['current_run'] for r in w.replans()}, {started['run_id']})
        self.assertEqual([ops.get_task(self.conn, tid)['attempts'] for tid in tids], [1, 1, 1])
        self.assertIsNotNone(method(self, w, 'replan_run')(started['run_id']))
        self.complete(w, requests, started)
        self.assertEqual([self.status(tid) for tid in tids], [states.READY] * 3)
        self.assertEqual([ops.get_task(self.conn, tid)['goal'] for tid in tids], [single.REVISE['goal']] * 3)
        self.assertEqual([len(self.attempts(tid)) for tid in tids], [1, 1, 1])

    def test_atomic_claim_race(self):
        w, tids, requests = self.requests()
        method(self, w, 'start_replans')
        barrier = threading.Barrier(2)
        def claim():
            conn = connect(self.path)
            try:
                barrier.wait(timeout=10)
                return Waiting(conn, 'dispatcher').start_replans(
                    [{'request_id': r['id'], 'inputs': self.inputs()} for r in requests], ROUTE, now=1000)
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.physical(tids)), 1)
        self.assertEqual([ops.get_task(self.conn, tid)['attempts'] for tid in tids], [1, 1])
        # One changed member rejects the entire enrollment, without a partial run.
        more = [self.task(), self.task()]
        later = [w.request_replan(tid, 'owner-request') for tid in more]
        ops.block(self.conn, more[1], 'independent', actor='owner')
        self.assertIsNone(self.enroll(w, later))
        self.assertEqual(self.physical(more), [])
        self.assertEqual([ops.get_task(self.conn, tid)['attempts'] for tid in more], [0, 0])

    def test_internal_waits_external_order(self):
        w = self.waiting()
        a, b, follower = [self.task() for _ in range(3)]
        ops.add_dep(self.conn, b, a)
        ops.add_dep(self.conn, follower, b)
        requests = [w.request_replan(tid, 'owner-request') for tid in (a, b)]
        before = [r['id'] for r in w.pending(('task', b))]
        self.assertIsNone(self.enroll(w, requests[1:]))
        started = self.enroll(w, requests)
        self.assertIsNotNone(started)
        self.assertEqual([r['id'] for r in w.pending(('task', b))], before)
        self.assertEqual(self.status(follower), states.BLOCKED)
        self.complete(w, requests, started)
        self.assertEqual(self.status(follower), states.READY)
        # A non-enrolled earlier prerequisite still blocks later planning.
        x, y = self.task(), self.task()
        ops.add_dep(self.conn, y, x)
        earlier, later = [w.request_replan(tid, 'owner-request') for tid in (x, y)]
        first = self.enroll(w, [earlier])
        self.assertIsNotNone(first)
        self.assertIsNone(self.enroll(w, [later]))

    def test_independent_limits(self):
        w, tids, requests = self.requests()
        ops.set_setting(self.conn, 'replan_limit', '1')
        first = self.enroll(w, requests[:1])
        self.complete(w, requests[:1], first, {}, outcome=contract.NO_RECORD)
        self.assertIsNone(self.enroll(w, requests[:1]))
        self.assertEqual(self.status(tids[0]), states.NEEDS_OWNER)
        second = self.enroll(w, requests[1:])
        self.assertIsNotNone(second)
        self.complete(w, requests[1:], second)
        self.assertEqual(self.status(tids[1]), states.READY)
        self.assertEqual([ops.get_task(self.conn, tid)['replans_used'] for tid in tids], [1, 1])

    def test_split_then_revise_uses_preapply_snapshots(self):
        w = self.waiting()
        a, b = self.task(), self.task()
        ops.add_dep(self.conn, b, a)
        requests = [w.request_replan(tid, 'owner-request') for tid in (a, b)]
        started = self.enroll(w, requests)
        self.complete(w, requests, started, {a: single.SPLIT, b: single.REVISE})
        self.assertEqual(self.status(a), states.CANCELLED)
        self.assertEqual(ops.get_task(self.conn, b)['goal'], single.REVISE['goal'])
        children = [d['depends_on'] for d in ops.deps_of(self.conn, b)]
        self.assertEqual(len(children), 2)
        self.assertEqual(self.status(b), states.BLOCKED)
        for child in children:
            self.assertEqual(self.status(child), states.PROPOSED)
            ops.set_status(self.conn, child, states.CANCELLED, actor='owner')
        self.assertEqual(self.status(b), states.READY)
        self.assertEqual(self.deadline(w, b), [])

    def test_invalid_member_savepoint(self):
        w, tids, requests = self.requests()
        started = self.enroll(w, requests)
        invalid = copy.deepcopy(single.SPLIT)
        invalid['tasks'][1]['deps'] = ['missing-child']
        before = {t['id'] for t in ops.list_tasks(self.conn)}
        self.complete(w, requests, started, {tids[0]: invalid, tids[1]: single.REVISE})
        self.assertEqual({t['id'] for t in ops.list_tasks(self.conn)}, before)
        self.assertEqual(ops.get_task(self.conn, tids[0])['goal'], 'Original')
        self.assertEqual(ops.get_task(self.conn, tids[0])['replans_used'], 0)
        self.assertTrue(self.deadline(w, tids[0]))
        self.assertEqual(ops.get_task(self.conn, tids[1])['goal'], single.REVISE['goal'])
        self.assertEqual(self.status(tids[1]), states.READY)
        self.assertEqual(self.physical(tids)[0]['outcome'], contract.DONE)
        self.assertEqual(self.attempts(tids[0])[0]['data']['outcome'], contract.UNVERIFIED)
        # Reject a domain operation after one child has actually been inserted.
        # A joined db.tx cannot undo that partial child; a member savepoint can.
        w, tids, requests = self.requests()
        started = self.enroll(w, requests)
        before = {t['id'] for t in ops.list_tasks(self.conn)}
        create = ops.create_task
        def reject_second(conn, title, *args, **kwargs):
            if title == 'Second':
                raise ValueError('split child rejected by domain validation')
            return create(conn, title, *args, **kwargs)
        with patch.object(ops, 'create_task', reject_second):
            self.complete(w, requests, started, {tids[0]: single.SPLIT, tids[1]: single.REVISE})
        self.assertEqual({t['id'] for t in ops.list_tasks(self.conn)}, before)
        self.assertEqual(ops.get_task(self.conn, tids[0])['replans_used'], 0)
        self.assertEqual(ops.get_task(self.conn, tids[1])['goal'], single.REVISE['goal'])

    def test_verify_replans_requeues_unchanged_member(self):
        w, tids, requests = self.requests()
        started = self.enroll(w, requests)
        changed = {**self.inputs(), 'dirty_digest': 'f' * 64}
        method(self, w, 'verify_replans')(started['run_id'],
            {requests[0]['id']: changed, requests[1]['id']: self.inputs()}, now=1001)
        self.assertEqual(self.physical(tids)[0]['outcome'], contract.UNVERIFIED)
        self.assertEqual({r['current_run'] for r in w.replans()}, {None})
        self.assertTrue(self.deadline(w, tids[0]))
        self.assertEqual(self.deadline(w, tids[1]), [])
        self.assertEqual([ops.get_task(self.conn, tid)['replans_used'] for tid in tids], [0, 0])

    def test_missing_invalid_results_retry_neutrally(self):
        w, tids, requests = self.requests(3)
        started = self.enroll(w, requests)
        self.complete(w, requests, started, {tids[0]: single.REVISE, tids[1]: {'action': 'delete'}})
        self.assertEqual(ops.get_task(self.conn, tids[0])['goal'], single.REVISE['goal'])
        self.assertEqual([ops.get_task(self.conn, tid)['replans_used'] for tid in tids], [1, 0, 0])
        self.assertEqual([ops.get_task(self.conn, tid)['goal'] for tid in tids[1:]], ['Original', 'Original'])
        self.assertTrue(all(self.deadline(w, tid) for tid in tids[1:]))
        self.assertEqual([self.attempts(tid)[0]['data']['outcome'] for tid in tids[1:]],
                         [contract.UNVERIFIED, contract.UNVERIFIED])

    def test_completion_database_rollback_and_replay(self):
        w, tids, requests = self.requests()
        started = self.enroll(w, requests)
        before = waiting_tests.logical(self.conn)
        # The second member fails after the first member has created split children.
        self.conn.execute("CREATE TRIGGER refuse_second BEFORE UPDATE OF goal ON tasks "
                          f"WHEN old.id='{tids[1]}' BEGIN SELECT RAISE(ABORT,'completion fault'); END")
        records = {tids[0]: single.SPLIT, tids[1]: single.REVISE}
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'completion fault'):
            self.complete(w, requests, started, records)
        self.assertEqual(waiting_tests.logical(self.conn), before)
        self.conn.execute('DROP TRIGGER refuse_second')
        completed = self.complete(w, requests, started, records)
        settled = waiting_tests.logical(self.conn)
        self.assertEqual(self.complete(w, requests, started, records), completed)
        self.assertEqual(waiting_tests.logical(self.conn), settled)

    def test_member_cancel_owner_resume_and_anchor_cancel(self):
        for disposition in ('cancel', 'owner-resume', 'archive'):
            with self.subTest(disposition=disposition):
                w, tids, requests = self.requests()
                started = self.enroll(w, requests)
                if disposition == 'cancel':
                    ops.set_status(self.conn, tids[0], states.CANCELLED, actor='owner')
                elif disposition == 'archive':
                    ops.archive_task(self.conn, tids[0])
                else:
                    ops.set_status(self.conn, tids[0], states.READY, actor='owner')
                self.complete(w, requests, started)
                self.assertEqual(ops.get_task(self.conn, tids[0])['goal'], 'Original')
                self.assertEqual(ops.get_task(self.conn, tids[1])['goal'], single.REVISE['goal'])
                self.assertEqual(self.status(tids[1]), states.READY)
                self.assertIsNotNone(self.physical(tids)[0]['ended'])
                if disposition == 'owner-resume':
                    event, = [e for e in ops.events(self.conn, kind='replan-completed', task_id=tids[0])]
                    self.assertEqual(event['data']['disposition'], 'owner-resolved')

    def test_all_members_protect_claim_and_delete(self):
        w, tids, requests = self.requests()
        started = self.enroll(w, requests)
        for tid in tids:
            ops.set_status(self.conn, tid, states.READY, actor='owner')
        self.assertIsNone(ops.claim_next(self.conn, {'actor': 'dispatcher'}))
        for tid in tids:
            with self.subTest(tid=tid), self.assertRaises(ValueError):
                ops.delete_task(self.conn, tid, confirm=True)
        self.complete(w, requests, started)
        self.assertIsNotNone(ops.claim_next(self.conn, {'actor': 'dispatcher'}))

    def test_usage_recorded_once(self):
        w, tids, requests = self.requests(3)
        started = self.enroll(w, requests)
        self.complete(w, requests, started)
        runs = self.physical(tids)
        self.assertEqual(len(runs), 1)
        self.assertEqual((sum(r['tokens_in'] for r in runs), sum(r['tokens_out'] for r in runs),
                          sum(r['tokens_cached'] for r in runs)), (101, 37, 11))
        self.assertEqual(runs[0]['allowance_used'], USAGE['allowance_used'])
        self.assertEqual(len(ops.events(self.conn, kind='run-finished')), 1)
        for tid in tids:
            event, = self.attempts(tid)
            self.assertEqual(event['data']['run'], started['run_id'])
            self.assertFalse({'tokens_in', 'tokens_out', 'allowance_used'} & event['data'].keys())


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
