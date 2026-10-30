"""Public ledger Waiting contract, independent of dispatcher implementation."""
import importlib
import json
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from tests.ledger_fixture import claim as fixture_claim, stage as fixture_stage
from pathlib import Path
from unittest.mock import patch

from drover import contract, dispatcher
from drover.ledger import connect, db, ops, schema
from drover.port import DirectPort
from tests.test_waiting import LedgerCase, logical, require

class Deadlines(LedgerCase):
    def test_timeout_owner_replacement(self):
        w = self.waiting()
        source, target = self.task(), self.task()
        ident = w.wait(('task', target), 'task-terminal', self.key(source), cause=self.key('task', source),
                       timeout_at=1200, data={'source': source, 'mode': 'blocked'})
        w.expire(1200)
        row, = w.pending(('task', target))
        self.assertEqual((row['kind'], row['cause']), ('owner-answer', self.key('timeout', ident)))
        self.assertEqual(self.status(target), 'needs-owner')
        q, = ops.open_questions(self.conn)
        w.expire(1500)
        self.assertEqual([r['id'] for r in ops.open_questions(self.conn)], [q['id']])
        queries = []
        self.conn.set_trace_callback(queries.append)
        try:
            w.expire(1600)
        finally:
            self.conn.set_trace_callback(None)
        due = [sql for sql in queries if sql.lstrip().upper().startswith('SELECT') and 'timeout_at' in sql]
        self.assertEqual(len(due), 1, due)
        plan = self.conn.execute('EXPLAIN QUERY PLAN ' + due[0]).fetchall()
        self.assertTrue(any('INDEX' in r['detail'].upper() for r in plan), [dict(r) for r in plan])
        ops.answer_question(self.conn, q['id'], text='The source is superseded; resume')
        self.assertEqual(self.status(target), 'ready')

    def test_backoff_and_review_recovery(self):
        w = self.waiting()
        tid, reviewed = self.task(), self.task('gated')
        now = 1700000000
        for minutes in (1, 2, 4, 8, 16, 30, 30):
            w.delay(tid, 'worker', 'admission', now)
            row, = w.pending(('task', tid))
            self.assertEqual((row['data']['minutes'], row['timeout_at']), (minutes, now + minutes * 60))
            self.conn.close()
            self.conn = connect(self.path)
            w = self.waiting()
            self.assertEqual(w.pending(('task', tid))[0]['timeout_at'], row['timeout_at'])
            count = len(ops.events(self.conn, task_id=tid, kind='status'))
            w.expire(now + minutes * 60 - 1)
            self.assertEqual(len(ops.events(self.conn, task_id=tid, kind='status')), count)
            w.expire(now + minutes * 60)
            now += minutes * 60
        w.delay(reviewed, 'review', 'provider', now, minutes=5)
        # Even disabled dispatch performs expiration before its early return.
        from tests.dispatch_fixture import make_dispatcher
        from tests.test_dispatcher import FakeLauncher
        ops.set_setting(self.conn, 'dispatch_enabled', 'false')
        d = make_dispatcher(DirectPort(self.conn), launcher=FakeLauncher(), now=lambda: now + 300)
        d.tick()
        self.assertEqual(w.pending(('task', reviewed)), [])
        self.assertEqual(self.status(reviewed), 'gated')
        fixture_claim(self.conn, tid)
        run = ops.start_run(self.conn, tid, 'worker', 'codex', 'gpt-6-luna')
        ops.finish_run(self.conn, run, 'provider-error')
        ops.set_status(self.conn, tid, 'ready', actor='dispatcher')
        w.delay(tid, 'worker', 'admission', now)
        self.assertEqual(w.pending(('task', tid))[0]['data']['minutes'], 1)


class FailureWindows(LedgerCase):
    def test_external_owner_reset_and_pin(self):
        w = self.waiting()
        tid, source = self.task(), self.task()
        pin = {'backend': 'codex', 'model': 'gpt-6-luna'}
        self.assertEqual(ops.update_task(self.conn, tid, model_pin=pin)['model_pin'], pin)
        for bad, actor in [({'backend': 'unknown', 'model': 'x'}, 'owner'),
                           ({'backend': 'codex', 'model': 'unknown'}, 'owner'),
                           ({**pin, 'effort': 'low'}, 'owner'), (pin, 'dispatcher')]:
            before = logical(self.conn)
            with self.assertRaises((ValueError, PermissionError)):
                ops.update_task(self.conn, tid, actor=actor, model_pin=bad)
            self.assertEqual(logical(self.conn), before)
        worker_ids = []
        for _ in range(2):
            rid = ops.start_run(self.conn, tid, 'worker', 'codex', 'gpt-6-luna')
            worker_ids.append(rid)
            ops.finish_run(self.conn, rid, 'error')
        unsettled = ops.start_run(self.conn, tid, 'worker', 'codex', 'gpt-6-luna')
        ops.finish_run(self.conn, unsettled, 'done')
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual(dispatcher.next_tier(runs, pin)['model'], 'gpt-6.1-sol')
        self.terminal_wait(w, tid, source)
        ops.block(self.conn, tid, 'independent', actor='owner')
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual(ops.get_task(self.conn, tid)['retry_after_run'], 0)
        ops.unblock(self.conn, tid, actor='owner')
        task = ops.get_task(self.conn, tid)
        self.assertEqual(task['retry_after_run'], worker_ids[-1])
        self.assertEqual(task['model_pin'], pin)
        self.assertEqual(dispatcher.next_tier(ops.list_runs(self.conn, tid), pin, task['retry_after_run']), pin)
        ops.amend_run(self.conn, unsettled, outcome='gate-fail')
        self.assertEqual(dispatcher.failures(ops.list_runs(self.conn, tid), task['retry_after_run']), 1)
        for actor in ('director', 'owner'):
            ops.set_status(self.conn, tid, 'needs-owner', reason='external', actor='system')
            ops.set_status(self.conn, tid, 'ready', actor=actor)
            self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)
            self.assertEqual(ops.get_task(self.conn, tid)['model_pin'], pin)
        # Route the pinned worker through the production dispatcher and router.
        from tests.dispatch_fixture import FakePipeline, Observations, make_dispatcher
        from tests.test_dispatcher import FakeLauncher
        from drover import config
        cfg = config.validate({})
        config.use(cfg)
        self.addCleanup(config.reset)
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        port = DirectPort(self.conn)
        launcher = FakeLauncher()
        observations = Observations(self.conn, lambda: 1700000000, lambda backend: {'remaining_pct': 90},
                                    lambda backend: backend == 'codex')
        d = make_dispatcher(port, pipeline=FakePipeline(port, Path(self.tmp.name) / 'runs'),
                            launcher=launcher, usage_poller=observations, now=lambda: 1700000000)
        self.assertEqual(d.tick()['launched'], [tid])
        self.assertEqual((launcher.specs[-1]['backend'], launcher.specs[-1]['model']), ('codex', 'gpt-6-luna'))
        self.assertIsNone(ops.update_task(self.conn, tid, model_pin={})['model_pin'])


class ReleaseRegressions(LedgerCase):
    def test_repeated_base_observations_do_not_supersede_closed_causes(self):
        w = self.waiting()
        tid = self.task()
        causes, questions = [], []
        for sha in ('a' * 40, 'b' * 40):
            cause = self.key('base', 'p', sha)
            causes.append(cause)
            w.wait(('merge_queue', 'p'), 'base', self.key('p', sha), cause=cause,
                   data={'repo': 'p', 'sha': sha, 'mode': 'preserve'})
            questions.append(w.ask(cause, 'task', tid, 'Base is red',
                                   options=[{'label': 'Retry landing'}], allow_text=False))
        # Observe the current base twice, then an older base twice.
        for sha, closed in (('b' * 40, 1), ('a' * 40, 2)):
            with self.subTest(sha=sha):
                ops.event(self.conn, 'base-observed', 'landing', {'repo': 'p', 'sha': sha})
                before = {kind: ops.events(self.conn, kind=kind)
                          for kind in ('question-superseded', 'waiting.clear')}
                self.assertEqual([len(events) for events in before.values()], [closed, closed])
                ops.event(self.conn, 'base-observed', 'landing', {'repo': 'p', 'sha': sha})
                for kind, events in before.items():
                    self.assertEqual(ops.events(self.conn, kind=kind), events)
        self.assertEqual([w.state(cause) for cause in causes], ['closed', 'closed'])
        self.assertEqual([ops.get_question(self.conn, q)['status'] for q in questions],
                         ['answered', 'answered'])
        self.assertEqual(w.pending(), [])

    def test_clear_only_records_outstanding_waits(self):
        w = self.waiting()
        tid = self.task()
        cause = self.key('manual', tid)
        before = logical(self.conn)
        self.assertEqual(w.clear(cause), [])
        self.assertEqual(logical(self.conn), before)
        ops.block(self.conn, tid, 'manual pause', actor='owner')
        self.assertEqual(w.clear(cause), [('task', tid)])
        self.assertEqual(self.status(tid), 'ready')
        self.assertEqual(len(ops.events(self.conn, kind='waiting.clear')), 1)
        before = logical(self.conn)
        self.assertEqual(w.clear(cause), [])
        self.assertEqual(logical(self.conn), before)

    def test_clear_released_retry_closes_cause_without_clear_event(self):
        w = self.waiting()
        cause, qid, choice = self.retry(w, ('task', self.task()))
        ops.answer_question(self.conn, qid, choice=choice)
        self.assertEqual(w.pending(), [])
        self.assertEqual(w.clear(cause), [])
        self.assertEqual(w.state(cause), 'closed')
        self.assertIsNone(w.answer(cause))
        self.assertFalse(w.consume_retry(cause))
        self.assertEqual(ops.events(self.conn, kind='waiting.clear'), [])
        self.assertEqual(len(ops.events(self.conn, kind='question-superseded')), 1)
        before = logical(self.conn)
        self.assertEqual(w.clear(cause), [])
        self.assertEqual(logical(self.conn), before)

    def test_base_success_resets_worker_window_and_budget(self):
        w = self.waiting()
        tid = self.task()
        rid = ops.start_run(self.conn, tid, 'worker', 'codex', 'gpt-6-luna')
        ops.finish_run(self.conn, rid, 'error')
        self.conn.execute('UPDATE tasks SET replans_used=2 WHERE id=?', (tid,))
        cause = self.key('base', 'p', 'a' * 40)
        w.wait(('task', tid), 'base', self.key('p', 'a' * 40), cause=cause,
               data={'repo': 'p', 'sha': 'a' * 40, 'mode': 'blocked'})
        ops.event(self.conn, 'base-ci', 'landing', {'repo': 'p', 'sha': 'a' * 40, 'status': 'green'})
        task = ops.get_task(self.conn, tid)
        self.assertEqual((task['status'], task['retry_after_run'], task['replans_used']), ('ready', rid, 0))

    def test_second_urgent_episode_requires_another_owner_answer(self):
        w = self.waiting()
        source, follower = self.task(), self.task()
        ops.add_dep(self.conn, follower, source)
        for _ in range(2):
            ops.add_comment(self.conn, 'task', source, 'owner', 'Pause downstream', urgent=True)
            self.assertEqual(self.status(follower), 'blocked')
            row, = w.pending(('task', follower))
            qid = row['question_id']
            self.assertEqual(ops.get_question(self.conn, qid)['status'], 'open')
            ops.answer_question(self.conn, qid, choice='Resume work')
            self.assertEqual(self.status(follower), 'ready')
        self.assertEqual(len(w.questions()), 0)

    def test_red_base_retry_reopens_the_canonical_question(self):
        w = self.waiting()
        tid = self.task()
        cause = self.key('base', 'p', 'a' * 40)
        payload = {'repo': 'p', 'sha': 'a' * 40, 'mode': 'preserve'}
        w.wait(('merge_queue', 'p'), 'base', self.key('p', 'a' * 40), cause=cause, data=payload)
        qid = w.ask(cause, 'task', tid, 'Retry base?',
                    options=[{'label': 'Retry landing'}, {'label': 'Keep holding'}], allow_text=False)
        ops.answer_question(self.conn, qid, choice='Retry landing')
        self.assertTrue(w.consume_retry(cause))
        ops.event(self.conn, 'base-ci', 'landing', {'repo': 'p', 'sha': 'a' * 40, 'status': 'red'})
        w.wait(('merge_queue', 'p'), 'base', self.key('p', 'a' * 40), cause=cause, data=payload)
        self.assertEqual((w.state(cause), ops.get_question(self.conn, qid)['status']), ('open', 'open'))
        self.assertFalse(w.consume_retry(cause))

    def test_reserved_events_release_and_guard_clear(self):
        w = self.waiting()
        tid = self.task()
        ops.block(self.conn, tid, 'manual pause', actor='owner')
        ops.event(self.conn, 'waiting.release', 'owner', {'cause': self.key('manual', tid)})
        self.assertEqual((self.status(tid), w.pending(('task', tid))), ('ready', []))
        cause, qid, choice = self.retry(w, ('task', tid))
        before = logical(self.conn)
        with self.assertRaises((ValueError, PermissionError)):
            ops.event(self.conn, 'waiting.clear', 'owner', {'cause': cause})
        self.assertEqual(logical(self.conn), before)
        ops.answer_question(self.conn, qid, choice=choice)
        ops.event(self.conn, 'waiting.clear', 'owner', {'cause': cause})
        self.assertEqual(w.state(cause), 'closed')
        self.assertIsNone(w.answer(cause))
        self.assertEqual(len(ops.events(self.conn, kind='waiting.released')), 2)

    def test_invalid_family_types_and_missing_request_roll_back(self):
        w = self.waiting()
        tid = self.task()
        for subject, kind, key, cause, data, error in [
            (('task', tid), 'base', self.key(7, 'a' * 40), self.key('base', 7, 'a' * 40),
             {'repo': 7, 'sha': 'a' * 40, 'mode': 'blocked'}, ValueError),
            (('task', tid), 'replan', self.key(123456), self.key('replan', 123456),
             {'request': 123456, 'source': tid, 'mode': 'blocked'}, ops.NotFound),
        ]:
            before = logical(self.conn)
            with self.subTest(kind=kind), self.assertRaises(error):
                w.wait(subject, kind, key, cause=cause, data=data)
            self.assertEqual(logical(self.conn), before)

    def test_recorded_owner_answer_cannot_be_replayed_as_a_new_grant(self):
        w = self.waiting()
        cause, qid, choice = self.retry(w, ('task', self.task()))
        ops.answer_question(self.conn, qid, choice=choice)
        self.assertTrue(w.consume_retry(cause))
        before = logical(self.conn)
        with self.assertRaises((ValueError, PermissionError)):
            ops.event(self.conn, 'question-answered', 'owner', {'question': qid, 'choice': choice})
        self.assertEqual(logical(self.conn), before)
        self.assertFalse(w.consume_retry(cause))

    def test_malformed_reserved_event_caught_inside_transaction_has_no_effect(self):
        before = logical(self.conn)
        with db.tx(self.conn):
            with self.assertRaises(ValueError):
                ops.event(self.conn, 'base-ci', 'landing', {'repo': 'p'})
        self.assertEqual(logical(self.conn), before)

    def test_live_base_question_cannot_be_cleared_directly(self):
        w = self.waiting()
        tid = self.task()
        cause = self.key('base', 'p', 'a' * 40)
        w.wait(('merge_queue', 'p'), 'base', self.key('p', 'a' * 40), cause=cause,
               data={'repo': 'p', 'sha': 'a' * 40, 'mode': 'preserve'})
        w.ask(cause, 'task', tid, 'Base is red',
              options=[{'label': 'Retry landing'}, {'label': 'Keep holding'}], allow_text=False)
        before = logical(self.conn)
        with self.assertRaises((ValueError, PermissionError)):
            w.clear(cause)
        self.assertEqual(logical(self.conn), before)
        ops.event(self.conn, 'base-observed', 'landing', {'repo': 'p', 'sha': 'b' * 40})
        self.assertEqual(w.pending(('merge_queue', 'p')), [])
