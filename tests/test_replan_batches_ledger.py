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
