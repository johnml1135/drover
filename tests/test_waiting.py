"""Public ledger Waiting contract, independent of dispatcher implementation."""
import importlib
import json
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import contract, dispatcher
from drover.ledger import connect, db, ops, schema
from drover.port import DirectPort


def require(case, module, symbol):
    try:
        obj = importlib.import_module(module)
    except ImportError as exc:
        case.fail(f'{module}.{symbol} interface is absent: {exc}')
    value = getattr(obj, symbol, None)
    case.assertTrue(callable(value), f'{module}.{symbol} interface is absent')
    return value


def logical(conn):
    """All logical rows, including append-only audit and migration versions."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: sorted((tuple(r) for r in conn.execute('SELECT * FROM "' + name + '"')), key=repr)
            for name in tables}


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.conn = connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.port = DirectPort(self.conn, actor='owner')

    def waiting(self, actor='owner', conn=None):
        factory = require(self, 'drover.ledger.waiting', 'Waiting')
        return factory(conn or self.conn, actor)

    def key(self, *parts):
        return require(self, 'drover.ledger.waiting', 'cause_key')(*parts)

    def task(self, state='ready', **fields):
        tid = ops.create_task(self.conn, fields.pop('title', 'work'), fields.pop('repo', 'p'),
                              read_access=[], **fields)['id']
        if state not in ('proposed', 'draft'):
            ops.approve(self.conn, tid)
            for next_state in {'ready': (), 'running': ('claimed', 'running'),
                               'gated': ('claimed', 'running', 'gated'),
                               'reviewed': ('claimed', 'running', 'gated', 'reviewed'),
                               'landed': ('claimed', 'running', 'gated', 'reviewed', 'landed'),
                               'cancelled': ('cancelled',)}[state]:
                ops.set_status(self.conn, tid, next_state, actor='owner')
        elif state == 'draft':
            ops.set_status(self.conn, tid, 'draft')
        return tid

    def status(self, tid):
        return ops.get_task(self.conn, tid)['status']

    def terminal_wait(self, waiting, target, source):
        return waiting.wait(('task', target), 'task-terminal', self.key(source),
                            cause=self.key('task', source), data={'source': source, 'mode': 'blocked'})

    def owner_wait(self, waiting, subject, family, *identity, mode='needs-owner'):
        cause = self.key(family, *identity)
        waiting.wait(subject, 'owner-answer', self.key(cause), cause=cause, data={'mode': mode})
        return cause

    def retry(self, waiting, subject, family='origin', repo='p'):
        cause = self.owner_wait(waiting, subject, family, repo)
        choice = 'Retry origin sync' if family == 'origin' else 'Retry landing'
        qid = waiting.ask(cause, 'task', subject[1] if subject[0] == 'task' else self.task(),
                          'Retry?', options=[{'label': choice}], allow_text=False)
        return cause, qid, choice


class Schema(LedgerCase):
    def test_atomic_v7(self):
        self.assertGreaterEqual(db.schema_version(self.conn), 7, 'Waiting V7 schema is absent')
        columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(tasks)')}
        self.assertTrue({'retry_after_run', 'replans_used', 'model_pin'} <= columns)
        expected = {
            'wait_subjects': {'subject_kind', 'subject_key', 'resume_state', 'reset_external', 'parked'},
            'wait_causes': {'cause', 'question_id', 'handler', 'state', 'answer', 'accepted_event',
                            'consumed_event', 'probe_session', 'probe_started', 'data'},
            'waits': {'id', 'subject_kind', 'subject_key', 'kind', 'key', 'cause', 'timeout_at',
                      'data', 'created_event', 'released_event'},
            'replan_requests': {'id', 'task_id', 'requesting_event', 'finished_event', 'current_run',
                                'state', 'disposition', 'snapshot', 'result'},
        }
        for table, names in expected.items():
            self.assertEqual({r['name'] for r in self.conn.execute(f'PRAGMA table_info({table})')}, names)
        pk = [r['name'] for r in self.conn.execute('PRAGMA table_info(base_gate)') if r['pk']]
        self.assertEqual(pk, ['repo', 'sha'])
        indexes = [r['sql'] or '' for r in self.conn.execute("SELECT sql FROM sqlite_master WHERE type='index'")]
        self.assertTrue(any('timeout_at' in x and 'WHERE' in x.upper() for x in indexes))
        self.assertTrue(any('UNIQUE' in x.upper() and 'released_event' in x for x in indexes))
        old = connect(Path(self.tmp.name) / 'old.db', migrate_db=False)
        self.addCleanup(old.close)
        db.migrate(old, migrations=[m for m in schema.MIGRATIONS if m[0] <= 6])
        before = logical(old)
        converter = require(self, 'drover.ledger.waiting_migration', 'migrate_waiting')
        with patch('drover.ledger.waiting_migration.migrate_waiting', side_effect=RuntimeError('conversion failed')):
            with self.assertRaisesRegex(RuntimeError, 'conversion failed'):
                db.migrate(old)
        self.assertEqual(logical(old), before)
        self.assertEqual(db.schema_version(old), 6)
        # A custom list must split complete statements, including a trigger's semicolons,
        # and must not invoke the real V7 Python converter merely because its version is 7.
        custom = [(7, '''CREATE TABLE custom(x INTEGER); CREATE TABLE audit(x INTEGER);
            CREATE TRIGGER custom_audit AFTER INSERT ON custom BEGIN
              INSERT INTO audit VALUES(new.x); INSERT INTO audit VALUES(new.x+1); END;
            INSERT INTO custom VALUES(4);''')]
        with patch('drover.ledger.waiting_migration.migrate_waiting', wraps=converter) as convert:
            db.migrate(old, custom)
            convert.assert_not_called()
        self.assertEqual([r[0] for r in old.execute('SELECT x FROM audit ORDER BY x')], [4, 5])


class Storage(LedgerCase):
    def test_multiple_causes(self):
        w = self.waiting()
        source = self.task()
        for initial in ('ready', 'proposed', 'draft'):
            tid = self.task(initial)
            ops.block(self.conn, tid, 'manual', actor='owner')
            first = self.terminal_wait(w, tid, source)
            self.assertEqual(self.terminal_wait(w, tid, source), first)
            self.assertEqual(len(w.pending(('task', tid))), 2)
            ops.unblock(self.conn, tid, actor='owner')
            self.assertEqual(self.status(tid), 'blocked')
            self.assertEqual(w.pending(('task', tid))[0]['resume_state'], initial)
            ops.archive_task(self.conn, tid)
            ops.archive_task(self.conn, tid, False)
            self.assertEqual(self.status(tid), 'blocked')
        targets = [t for t in ops.list_tasks(self.conn) if t['id'] != source]
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        for tid, initial in zip([t['id'] for t in targets], ('ready', 'proposed', 'draft')):
            self.assertEqual(self.status(tid), initial)
            self.assertEqual(w.pending(('task', tid)), [])
            self.assertEqual(bool(ops.get_task(self.conn, tid)['approved_at']), initial == 'ready')
            # A second episode captures the current state, rather than stale resume metadata.
            ops.block(self.conn, tid, 'again', actor='owner')
            ops.unblock(self.conn, tid, actor='owner')
            self.assertEqual(self.status(tid), initial)

    def test_registration_validation(self):
        w = self.waiting()
        tid, source = self.task(), self.task()
        valid = dict(cause=self.key('task', source), data={'source': source, 'mode': 'blocked'})
        for kind, key, extra in [
            ('unknown', self.key(source), valid),
            ('task-terminal', self.key(source), {**valid, 'data': {'source': source, 'mode': 'blocked', 'extra': 1}}),
            ('task-terminal', 'not json', valid),
            ('task-terminal', self.key(source), {**valid, 'cause': self.key('unknown', source)}),
            ('task-terminal', self.key(source), {**valid, 'timeout_at': True}),
            ('task-terminal', self.key(source), {**valid, 'timeout_at': float('nan')}),
        ]:
            before = logical(self.conn)
            with self.subTest(kind=kind, extra=extra), self.assertRaises(ValueError):
                w.wait(('task', tid), kind, key, **extra)
            self.assertEqual(logical(self.conn), before)
        before = logical(self.conn)
        with self.assertRaises(ops.NotFound):
            self.terminal_wait(w, 'T-missing', source)
        self.assertEqual(logical(self.conn), before)
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.terminal_wait(w, tid, source)
        self.assertEqual(w.pending(('task', tid)), [])
        self.assertEqual(self.status(tid), 'ready')
        reviewed = self.task('reviewed')
        self.owner_wait(w, ('merge_queue', 'p'), 'dirty', 'p', mode='blocked')
        self.assertEqual(self.status(reviewed), 'reviewed')
        self.assertEqual(w.state(self.key('origin', 'never-seen')), None)


class Kinds(LedgerCase):
    def test_task_terminal_release(self):
        w = self.waiting()
        other = self.task()
        untouched = self.task()
        self.terminal_wait(w, untouched, other)
        for terminal in ('landed', 'cancelled'):
            source = self.task('reviewed' if terminal == 'landed' else 'ready')
            target = self.task()
            self.terminal_wait(w, target, source)
            with db.tx(self.conn):
                ops.set_status(self.conn, source, terminal, actor='owner')
                self.assertEqual(w.pending(('task', target)), [])
                self.assertEqual(self.status(target), 'ready')
            release = [e for e in ops.events(self.conn, task_id=target, kind='waiting.released')]
            self.assertEqual(release[-1]['actor'], 'owner')
            self.terminal_wait(w, target, source)
            self.assertEqual(w.pending(('task', target)), [])
        self.assertEqual(self.status(untouched), 'blocked')
        ops.delete_task(self.conn, other, confirm=True)
        self.assertEqual(self.status(untouched), 'ready')

    def test_manual_release(self):
        w = self.waiting()
        target, source = self.task(), self.task()
        ops.block(self.conn, target, 'replan; urgent; source finished', actor='owner')
        self.terminal_wait(w, target, source)
        ops.unblock(self.conn, target, actor='owner')
        self.assertEqual([r['kind'] for r in w.pending(('task', target))], ['task-terminal'])
        self.assertEqual(self.status(target), 'blocked')
        with self.assertRaises(ValueError):
            w.release(self.key('task', source))
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual(self.status(target), 'ready')

    def test_base_move_green_retry_release(self):
        w = self.waiting()
        for resolution in ('move', 'green', 'retry'):
            repo, sha = 'p-' + resolution, 'a' * 40
            cause = self.key('base', repo, sha)
            w.wait(('merge_queue', repo), 'base', self.key(repo, sha), cause=cause,
                   data={'repo': repo, 'sha': sha, 'mode': 'preserve'})
            tid = self.task()
            w.wait(('task', tid), 'base', self.key(repo, sha), cause=cause,
                   data={'repo': repo, 'sha': sha, 'mode': 'blocked'})
            qid = w.ask(cause, 'task', tid, 'Base is red', options=[{'label': 'Retry landing'}, {'label': 'Keep holding'}],
                        allow_text=False)
            ops.answer_question(self.conn, qid, choice='Keep holding')
            self.assertTrue(w.pending(('merge_queue', repo)))
            self.assertEqual(ops.get_question(self.conn, qid)['status'], 'open')
            ops.event(self.conn, 'base-ci', 'landing', {'repo': 'another', 'sha': sha, 'status': 'green'})
            ops.event(self.conn, 'base-ci', 'landing', {'repo': repo, 'sha': 'b' * 40, 'status': 'green'})
            self.assertTrue(w.pending(('merge_queue', repo)))
            if resolution == 'move':
                ops.event(self.conn, 'base-observed', 'landing', {'repo': repo, 'sha': 'b' * 40})
            elif resolution == 'green':
                ops.event(self.conn, 'base-ci', 'landing', {'repo': repo, 'sha': sha, 'status': 'green'})
            else:
                ops.answer_question(self.conn, qid, choice='Retry landing')
                self.assertTrue(w.consume_retry(cause))
                self.assertFalse(w.consume_retry(cause))
            self.assertEqual(w.pending(('merge_queue', repo)), [])
            self.assertEqual(self.status(tid), 'ready')
            self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)

    def test_deadline_release(self):
        w = self.waiting()
        for stage, role in [('ready', 'worker'), ('gated', 'review')]:
            tid = self.task(stage)
            w.delay(tid, role, 'provider', 1000, minutes=5)
            self.assertEqual(self.status(tid), stage)
            self.assertEqual(w.expire(1299), [])
            self.assertIn(('task', tid), w.expire(1300))
            self.assertEqual(self.status(tid), stage)
            self.assertEqual(w.pending(('task', tid)), [])
        tid = self.task()
        w.delay(tid, 'worker', 'wsl-down', 2000)
        ops.event(self.conn, 'capacity-available', 'watchdog', {'placement': 'windows'})
        self.assertTrue(w.pending(('task', tid)))
        ops.event(self.conn, 'capacity-available', 'watchdog', {'placement': 'wsl'})
        self.assertEqual(w.pending(('task', tid)), [])
        self.assertEqual(self.status(tid), 'ready')


class Events(LedgerCase):
    def test_nested_and_rollback(self):
        w = self.waiting()
        source, a, b = self.task(), self.task(), self.task()
        self.terminal_wait(w, a, source)
        self.terminal_wait(w, b, source)
        before = logical(self.conn)
        self.conn.execute(f"CREATE TRIGGER refuse_restore BEFORE UPDATE OF status ON tasks WHEN old.id='{a}' AND new.status='ready' BEGIN SELECT RAISE(ABORT,'restore failed'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual(logical(self.conn), before)
        self.conn.execute('DROP TRIGGER refuse_restore')
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with db.tx(self.conn):
                ops.set_status(self.conn, source, 'cancelled', actor='owner')
                self.assertEqual(self.status(a), 'ready')
                raise RuntimeError('abort')
        self.assertEqual(logical(self.conn), before)
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual([self.status(t) for t in (a, b)], ['ready'] * 2)
        self.assertEqual(len(ops.events(self.conn, kind='waiting.released')), 2)
        ident = ops.event(self.conn, 'unrelated-history', 'director', {'free': 'shape'}, task_id=a)
        self.assertIsInstance(ident, int)
        for kind, data in [('base-ci', {'repo': 'p'}), ('waiting.release', {}),
                           ('replan-completed', {'request': 'bad'}), ('capacity-available', {'placement': 'unknown'})]:
            before = logical(self.conn)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                ops.event(self.conn, kind, 'system', data, task_id=a)
            self.assertEqual(logical(self.conn), before)


class Questions(LedgerCase):
    def test_shared_owner_answer_release(self):
        w = self.waiting()
        tids = [self.task() for _ in range(3)]
        cause = self.key('script', 'p')
        for tid in tids:
            self.owner_wait(w, ('task', tid), 'script', 'p')
        q = w.ask(cause, 'task', tids[0], 'Supply test script')
        self.assertEqual(w.ask(cause, 'task', tids[0], 'Different display title'), q)
        source = self.task()
        self.terminal_wait(w, tids[-1], source)
        self.assertEqual({r['handler'] for r in w.pending() if r['cause'] == cause}, {'script'})
        for invalid in ('../escape.ps1', '/tmp/escape.ps1', 'C:\\escape.ps1', 'test.sh', 'a.ps1; rm'):
            before = logical(self.conn)
            with self.subTest(script=invalid), self.assertRaises(ValueError):
                ops.answer_question(self.conn, q, text=invalid)
            self.assertEqual(logical(self.conn), before)
        with self.assertRaises(ValueError):
            w.ask(cause, 'task', tids[1], 'Incompatible target')
        with self.assertRaises(ValueError):
            w.ask(cause, 'task', tids[0], 'Incompatible options', options=[{'label': 'Skip checks'}])
        ops.answer_question(self.conn, q, text='.drover/scripts/test.ps1')
        self.assertEqual([self.status(t) for t in tids], ['ready', 'ready', 'blocked'])
        answer = w.answer(cause)
        self.assertEqual((answer['question'], answer['actor'], answer['text']), (q, 'owner', '.drover/scripts/test.ps1'))
        self.assertIsInstance(answer['event'], int)
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual(self.status(tids[-1]), 'ready')

    def test_bypasses_and_keep_holding(self):
        w = self.waiting()
        tid = self.task()
        cause = self.owner_wait(w, ('task', tid), 'urgent', 'task', tid)
        q = w.ask(cause, 'task', tid, 'Pause', options=[{'label': 'Resume work'}, {'label': 'Keep holding'}], allow_text=False)
        before = logical(self.conn)
        with self.assertRaises(PermissionError):
            ops.answer_question(self.conn, q, choice='Resume work', actor='dispatcher')
        self.assertEqual(logical(self.conn), before)
        for actor in ('dispatcher', 'owner'):
            with self.assertRaises((ValueError, PermissionError)):
                ops.event(self.conn, 'question-answered', actor, {'question': q, 'choice': 'Resume work'}, task_id=tid)
        with self.assertRaises((ValueError, PermissionError)):
            ops.set_status(self.conn, tid, 'ready', actor='owner')
        ops.add_comment(self.conn, 'task', tid, 'owner', 'ordinary comment')
        self.assertEqual(self.status(tid), 'needs-owner')
        ops.answer_question(self.conn, q, choice='Keep holding')
        self.assertTrue(w.pending(('task', tid)))
        self.assertEqual(ops.get_question(self.conn, q)['status'], 'open')
        ops.answer_question(self.conn, q, choice='Resume work')
        self.assertEqual(self.status(tid), 'ready')
        plain = self.task()
        ops.set_status(self.conn, plain, 'needs-owner', reason='ordinary review', actor='system')
        ops.add_comment(self.conn, 'task', plain, 'owner', 'go ahead')
        self.assertEqual(self.status(plain), 'ready')
        a = ops.ask_question(self.conn, 'task', plain, 'One?', needs_owner=True)
        b = ops.ask_question(self.conn, 'task', plain, 'Two?', needs_owner=True)
        ops.answer_question(self.conn, a, text='one')
        self.assertEqual(self.status(plain), 'needs-owner')
        ops.answer_question(self.conn, b, text='two')
        self.assertEqual(self.status(plain), 'ready')

    def test_release_clear_and_retry_claim(self):
        w = self.waiting()
        tid = self.task()
        cause, q, choice = self.retry(w, ('task', tid))
        with self.assertRaises((ValueError, PermissionError)):
            w.clear(cause)
        ops.answer_question(self.conn, q, choice=choice)
        self.assertEqual(w.pending(('task', tid)), [])
        self.assertEqual(w.state(cause), 'authorized')
        self.assertIsNotNone(w.answer(cause))
        second = connect(self.path)
        self.addCleanup(second.close)
        w2 = self.waiting(conn=second)
        barrier = threading.Barrier(2)
        def claim():
            conn = connect(self.path)
            try:
                contender = self.waiting(conn=conn)
                barrier.wait(timeout=10)
                return contender.consume_retry(cause)
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        late = self.task()
        self.owner_wait(w2, ('task', late), 'origin', 'p')
        self.assertTrue(w.pending(('task', late)))
        self.assertEqual(len(ops.open_questions(self.conn)), 0)
        ops.event(self.conn, 'origin-synced', 'dispatcher', {'repo': 'p'})
        self.assertEqual(self.status(late), 'ready')
        self.assertEqual(w.state(cause), 'resolved')
        w.clear(cause)
        self.assertIsNone(w.answer(cause))
        self.assertFalse(w.consume_retry(cause))
        # A different process may adopt CI, but may never replay a consumed approval.
        cause2, q2, choice2 = self.retry(w, ('task', self.task()), repo='other')
        ops.answer_question(self.conn, q2, choice=choice2)
        self.assertTrue(w.consume_retry(cause2))
        with patch('drover.ledger.waiting.PROCESS_SESSION', 'a-new-process'):
            self.assertFalse(self.waiting(conn=second).consume_retry(cause2))
        self.assertEqual(ops.get_question(self.conn, q2)['status'], 'open')
        ops.answer_question(self.conn, q2, choice=choice2)
        self.assertTrue(w.consume_retry(cause2))
        ops.event(self.conn, 'origin-sync-failed', 'dispatcher', {'repo': 'other', 'reason': 'offline'})
        self.assertEqual(ops.get_question(self.conn, q2)['status'], 'open')
        self.assertIsNotNone(w.answer(cause2))


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
        ops.set_status(self.conn, tid, 'claimed')
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
