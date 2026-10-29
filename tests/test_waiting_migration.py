"""V6 ledgers assembled locally; no owner ledger or machine config is inspected."""
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from drover.ledger import connect, db, ops, schema
from tests.test_waiting import logical, require

STAMP = '2026-10-20T12:00:00Z'


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'v6.db'
        self.conn = connect(self.path, migrate_db=False)
        self.addCleanup(lambda: self.conn.close())
        db.migrate(self.conn, [m for m in schema.MIGRATIONS if m[0] <= 6])
        self.sequence = 0

    def converter(self):
        return require(self, 'drover.ledger.waiting_migration', 'migrate_waiting')

    def task(self, *, status='ready', approved=True, blocker=None, archived=False, repo='p', epic=None):
        self.sequence += 1
        tid = f'T-fixture-{self.sequence}'
        self.conn.execute('INSERT INTO tasks(id,repo,title,rank,status,created,updated,approved_at,blocker,archived,epic_id) '
                          'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (tid, repo, 'fixture work', self.sequence, status, STAMP, STAMP,
                           STAMP if approved else None, blocker if isinstance(blocker, str) else
                           json.dumps(blocker) if blocker is not None else None, int(archived), epic))
        return tid

    def epic(self, repo='p'):
        self.sequence += 1
        eid = f'E-fixture-{self.sequence}'
        self.conn.execute('INSERT INTO epics(id,repo,title,rank,created,updated) VALUES(?,?,?,?,?,?)',
                          (eid, repo, 'fixture epic', self.sequence, STAMP, STAMP))
        return eid

    def event(self, kind, data, *, task=None, epic=None, actor='owner', at=STAMP):
        return self.conn.execute('INSERT INTO task_events(kind,actor,data,task_id,epic_id,at) VALUES(?,?,?,?,?,?)',
                                 (kind, actor, json.dumps(data), task, epic, at)).lastrowid

    def setting(self, key, value):
        self.conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                          (key, value if isinstance(value, str) else json.dumps(value)))

    def question(self, target, *, kind='task', title='fixture question', options=(), answer=None,
                 actor='owner', text=None):
        qid = self.conn.execute('INSERT INTO questions(target_kind,target_id,title,options,allow_text,asked,status,'
                                'answer_choice,answer_text,answered_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                (kind, target, title, json.dumps([{'label': x, 'description': '', 'recommended': False}
                                                                 for x in options]), 1, STAMP,
                                 'answered' if answer or text else 'open', answer, text,
                                 STAMP if answer or text else None)).lastrowid
        self.event('question-asked', {'question': qid, 'title': title},
                   task=target if kind == 'task' else None, epic=target if kind == 'epic' else None, actor='system')
        if answer or text:
            self.event('question-answered', {'question': qid, 'choice': answer},
                       task=target if kind == 'task' else None, epic=target if kind == 'epic' else None, actor=actor)
        return qid

    def hold(self, tid, holds, prev='ready'):
        self.conn.execute('UPDATE tasks SET status=?,blocker=? WHERE id=?',
                          ('blocked', json.dumps({'holds': holds, 'prev': prev}), tid))

    def migrate(self):
        self.converter()
        reports = []
        converter = self.converter()
        def capture(conn):
            report = converter(conn)
            reports.append(report)
            return report
        with patch('drover.ledger.waiting_migration.migrate_waiting', side_effect=capture):
            db.migrate(self.conn)
        self.assertEqual(db.schema_version(self.conn), max(version for version, _ in schema.MIGRATIONS))
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(set(report), {'converted', 'retired', 'diagnostics'})
        self.assertEqual(set(report['converted']), {'holds', 'causes', 'questions', 'requests', 'deadlines', 'cache'})
        for diagnostic in report['diagnostics']:
            self.assertEqual(set(diagnostic), {'location', 'code', 'raw', 'subjects'})
        emitted = [e['data'] for e in ops.events(self.conn, kind='waiting.migration-diagnostic')]
        for diagnostic in report['diagnostics']:
            self.assertIn(diagnostic, emitted)
        return report, require(self, 'drover.ledger.waiting', 'Waiting')(self.conn, 'owner')

    def key(self, *parts):
        return json.dumps(parts, ensure_ascii=True, separators=(',', ':'))


class HoldsMigration(MigrationCase):
    def test_276_live_shaped_holds(self):
        self.converter()
        sources = [self.task(status='blocked') for _ in range(55)]
        followers = [self.task(status='blocked', approved=n % 3 == 0, archived=n % 11 == 0) for n in range(221)]
        for tid in sources:
            self.hold(tid, [{'source': tid, 'reason': 'replan'}])
            self.event('replan-request', {'trigger': 'blocker'}, task=tid, actor='dispatcher')
        for n, tid in enumerate(followers):
            holds = [{'source': sources[n % 55], 'reason': 'replan'}]
            if n % 17 == 0:
                holds.append({'source': 'manual', 'reason': 'owner work'})
            if n % 19 == 0:
                holds.append({'source': sources[(n + 1) % 55], 'reason': 'urgent'})
            self.hold(tid, holds, ('ready', 'proposed', 'draft')[n % 3])
        report, w = self.migrate()
        self.assertEqual(len(ops.list_tasks(self.conn, include_archived=True)), 276)
        self.assertEqual(len(w.replans()), 55)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks WHERE blocker IS NOT NULL').fetchone()[0], 0)
        self.assertEqual(report['diagnostics'], [])
        self.assertEqual(report['converted']['requests'], 55)
        source = sources[0]
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        for n, tid in enumerate(followers):
            waits = w.pending(('task', tid))
            if n % 55 == 0 and n % 17 != 0 and n % 19 != 0:
                self.assertEqual(waits, [])
                self.assertEqual(ops.get_task(self.conn, tid)['status'], ('ready', 'proposed', 'draft')[n % 3])
            elif n % 55 != 0:
                self.assertTrue(waits)
        untouched = next(t for n, t in enumerate(followers) if n % 55 == 0 and n % 17 == 0)
        self.assertIn('manual', [row['kind'] for row in w.pending(('task', untouched))])

    def test_owner_ready_boundary_retires_stale_replan_hold(self):
        source = self.task()
        target = self.task(status='blocked', blocker={'source': source, 'reason': 'replan', 'prev': 'ready'})
        self.event('replan-request', {}, task=source)
        self.event('status', {'from': 'blocked', 'to': 'ready'}, task=source, actor='owner')
        report, w = self.migrate()
        self.assertEqual(w.replans(), [])
        self.assertEqual(w.pending(('task', target)), [])
        self.assertEqual(ops.get_task(self.conn, source)['status'], 'ready')
        self.assertEqual(ops.get_task(self.conn, target)['status'], 'ready')
        self.assertTrue(any(r['disposition'] == 'stale-replan-hold' for r in report['retired']))

    def test_legacy_shapes_and_diagnostics(self):
        self.converter()
        source = self.task()
        manual = self.task(status='blocked', blocker={'reason': 'owner text', 'prev': 'ready'})
        old = self.task(status='blocked', blocker={'source': source, 'reason': 'arbitrary words', 'prev': 'ready'})
        terminal = self.task(status='cancelled')
        released = self.task(status='blocked', blocker={'source': terminal, 'reason': 'waiting', 'prev': 'ready'})
        deleted = 'T-deleted'
        self.event('task.deleted', {'id': deleted})
        gone = self.task(status='blocked', blocker={'source': deleted, 'reason': 'waiting', 'prev': 'ready'})
        malformed = []
        for raw in ('{broken', '[]', 'null', '3', '{}', '{"holds":[]}',
                    '{"holds":"bad"}', '{"holds":[1],"prev":"ready"}',
                    '{"holds":[{"source":"missing","reason":"replan"}],"prev":"ready"}',
                    '{"holds":[{"source":"manual","reason":"x"}],"prev":"unknown"}'):
            malformed.append((self.task(status='blocked', blocker=raw), raw))
        null = self.task(status='blocked')
        independent = self.task(status='blocked')
        self.hold(independent, [{'source': source, 'reason': 'waiting'}, {'source': 7, 'reason': 'bad'}])
        report, w = self.migrate()
        self.assertEqual(ops.get_task(self.conn, released)['status'], 'ready')
        self.assertEqual(ops.get_task(self.conn, gone)['status'], 'ready')
        self.assertEqual([r['kind'] for r in w.pending(('task', old))], ['task-terminal'])
        self.assertEqual([r['kind'] for r in w.pending(('task', manual))], ['manual'])
        for tid, raw in malformed:
            entries = [d for d in report['diagnostics'] if d['location'].startswith(f'tasks/{tid}/blocker')]
            self.assertTrue(entries, tid)
            self.assertTrue(any(d['raw'] == raw or d['raw'] == json.loads(raw) for d in entries)
                            if raw not in ('{broken',) else entries[0]['raw'] == raw)
            self.assertTrue(w.pending(('task', tid)))
        self.assertTrue(any(d['location'] == f'tasks/{null}/blocker' for d in report['diagnostics']))
        quarantine = next(r for r in w.pending(('task', independent)) if r['kind'] == 'owner-answer')
        ops.answer_question(self.conn, quarantine['question_id'], text='Acknowledge legacy ambiguity')
        self.assertEqual([r['kind'] for r in w.pending(('task', independent))], ['task-terminal'])
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual(ops.get_task(self.conn, independent)['status'], 'ready')


class CopyMigration(MigrationCase):
    def test_deadline_cache_and_release(self):
        self.converter()
        now = datetime.now(timezone.utc).timestamp()
        future, expired, invalid = self.task(), self.task(), self.task()
        self.setting('_dispatch_backoff', {future: {'until': now + 1200, 'min': 8},
                                           expired: {'until': now - 10, 'min': 1},
                                           invalid: {'until': True, 'min': 1}})
        review = self.task(status='gated')
        ended = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.conn.execute('INSERT INTO runs(task_id,role,backend,started,ended,outcome) VALUES(?,?,?,?,?,?)',
                          (review, 'review', 'codex', ended, ended, 'provider-error'))
        task = self.task(status='reviewed')
        q = self.question(task, options=['Retry landing', 'Keep holding'])
        self.setting('landing_hold', 'a' * 40)
        self.setting('landing_hold_question', str(q))
        self.event('base-red', {'sha': 'a' * 40, 'question': q, 'tasks': [task]}, actor='landing')
        for sha in ('a' * 40, 'b' * 40):
            self.conn.execute('INSERT INTO base_gate(sha,status,at) VALUES(?,?,?)', (sha, 'red', STAMP))
        report, w = self.migrate()
        row, = w.pending(('task', future))
        self.assertEqual((row['timeout_at'], row['data']['minutes']), (now + 1200, 8))
        self.assertEqual(w.pending(('task', expired)), [])
        row, = w.pending(('task', review))
        self.assertEqual(row['timeout_at'], datetime.strptime(ended, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp() + 300)
        self.assertTrue(any(d['code'] == 'cache-unscoped' and d['location'] == 'base_gate/' + 'b' * 40 for d in report['diagnostics']))
        self.assertTrue(w.pending(('task', invalid)))
        self.assertEqual(self.conn.execute('SELECT repo,sha,status FROM base_gate').fetchall()[0][:], ('p', 'a' * 40, 'red'))
        self.assertFalse(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='base_gate_legacy'").fetchone())
        w.expire(now + 1200)
        self.assertEqual(w.pending(('task', future)), [])
        self.assertEqual(ops.get_task(self.conn, future)['status'], 'ready')
        ops.event(self.conn, 'base-observed', 'landing', {'repo': 'p', 'sha': 'c' * 40})
        self.assertEqual(w.pending(('merge_queue', 'p')), [])

    def test_idempotence(self):
        self.converter()
        source = self.task()
        target = self.task(status='blocked', blocker={'source': source, 'reason': 'waiting', 'prev': 'ready'})
        self.setting('unrelated', 'preserve me')
        report, w = self.migrate()
        before = logical(self.conn)
        self.conn.close()
        self.conn = connect(self.path)
        db.migrate(self.conn)
        second = self.converter()(self.conn)
        self.assertEqual(second['diagnostics'], [])
        self.assertEqual(logical(self.conn), before)
        w = require(self, 'drover.ledger.waiting', 'Waiting')(self.conn, 'owner')
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        event = ops.events(self.conn, task_id=source, kind='status')[-1]
        after = logical(self.conn)
        w.resolve(event)
        w.resolve(event)
        self.assertEqual(logical(self.conn), after)
        self.assertEqual(ops.get_task(self.conn, target)['status'], 'ready')

    def test_resolve_standalone_transaction_and_replay(self):
        source = self.task()
        target = self.task(status='blocked', blocker={'source': source, 'reason': 'waiting', 'prev': 'ready'})
        _, w = self.migrate()
        with patch.object(type(w), 'resolve', return_value=[]):
            ops.set_status(self.conn, source, 'cancelled', actor='owner')
        event = ops.events(self.conn, task_id=source, kind='status')[-1]
        before = logical(self.conn)
        with patch.object(w, '_restore_subject', side_effect=RuntimeError('injected restoration')):
            with self.assertRaisesRegex(RuntimeError, 'injected restoration'):
                w.resolve(event)
        self.assertEqual(logical(self.conn), before)
        self.assertEqual(w.resolve(event), [('task', target)])
        after = logical(self.conn)
        self.assertEqual(w.resolve(event), [])
        self.assertEqual(logical(self.conn), after)
        self.assertFalse(self.conn.in_transaction)

    def test_module_cli_reports_conversion_counts(self):
        import os
        import subprocess
        self.task(status='blocked', blocker={'reason': 'manual', 'prev': 'ready'})
        self.conn.close()
        result = subprocess.run([os.sys.executable, '-m', 'drover.ledger.waiting_migration',
                                 '--source', str(self.path), '--copy', str(Path(self.tmp.name) / 'cli.db'),
                                 '--verify-copy'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report['migration']), 1)
        self.assertEqual(report['migration'][0]['converted']['holds'], 1)
        self.conn = connect(self.path, migrate_db=False)

    def test_atomic_rollback(self):
        converter = self.converter()
        tid = self.task(status='blocked', blocker={'reason': 'manual', 'prev': 'ready'})
        q = self.question(tid)
        self.setting('_gate_question_p', str(q))
        self.setting('_dispatch_backoff', {})
        before = logical(self.conn)
        ddl = list(self.conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))
        for boundary in ('_holds', '_questions', '_deadlines'):
            module = __import__('drover.ledger.waiting_migration', fromlist=[boundary])
            original = getattr(module, boundary, None)
            self.assertTrue(callable(original), f'{boundary} conversion boundary is absent')
            def explode(conn, report, original=original):
                original(conn, report)
                raise RuntimeError('injected boundary')
            with patch.object(module, boundary, side_effect=explode):
                with self.assertRaisesRegex(RuntimeError, 'injected boundary'):
                    db.migrate(self.conn)
            self.assertEqual(logical(self.conn), before)
            self.assertEqual([tuple(r) for r in self.conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')],
                             [tuple(r) for r in ddl])
        self.conn.execute("CREATE TRIGGER refuse_v7 BEFORE INSERT ON schema_version WHEN new.version=7 BEGIN SELECT RAISE(ABORT,'injected version'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            db.migrate(self.conn)
        self.assertEqual(logical(self.conn), before)
        self.conn.execute('DROP TRIGGER refuse_v7')
        db.migrate(self.conn)
        self.assertEqual(db.schema_version(self.conn), max(version for version, _ in schema.MIGRATIONS))

    def test_readonly_backup_and_release_rehearsal(self):
        main = require(self, 'drover.ledger.waiting_migration', 'main')
        source = self.task()
        self.task(status='blocked', blocker={'source': source, 'reason': 'waiting', 'prev': 'ready'})
        self.task(status='blocked', blocker={'reason': 'owner local work', 'prev': 'ready'})
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        original = logical(self.conn)
        self.conn.close()
        original_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        destination = Path(self.tmp.name) / 'converted.db'
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['--source', str(self.path), '--copy', str(destination), '--verify-copy'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), original_hash)
        self.conn = connect(self.path, migrate_db=False)
        self.assertEqual(logical(self.conn), original)
        self.assertEqual(db.schema_version(self.conn), 6)
        copy = connect(destination)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(copy.execute('PRAGMA foreign_key_check').fetchall(), [])
        before = logical(copy)
        db.migrate(copy)
        self.assertEqual(logical(copy), before)
        self.assertTrue(require(self, 'drover.ledger.waiting', 'Waiting')(copy, 'owner').pending())
        for dest in (destination, self.path):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--source', str(self.path), '--copy', str(dest), '--verify-copy']), 2)
        # Operational rollback uses the unchanged V6 backup with the old migration list.
        db.migrate(self.conn, [m for m in schema.MIGRATIONS if m[0] <= 6])
        self.assertEqual(logical(self.conn), original)
