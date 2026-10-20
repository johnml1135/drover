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
        self.assertEqual(db.schema_version(self.conn), 7)
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


class QuestionsMigration(MigrationCase):
    def seed_question_keys(self):
        script = self.task(status='needs-owner')
        qs = self.question(script, title='What is the test script path for p?')
        self.setting('_gate_question_p', str(qs))
        origin_task = self.task(status='needs-owner', repo='offline')
        qo = self.question(origin_task, options=['Retry origin sync'])
        self.setting('_origin_pause_offline', {'question': qo})
        dirty_task = self.task(status='reviewed', repo='dirty')
        qd = self.question(dirty_task, options=['Retry landing'])
        self.setting('_land_dirty_q_dirty', str(qd))
        base_task = self.task(status='reviewed', repo='base')
        qb = self.question(base_task, options=['Retry landing', 'Keep holding'])
        self.setting('landing_hold', 'a' * 40)
        self.setting('landing_hold_question', str(qb))
        self.event('base-red', {'sha': 'a' * 40, 'question': qb, 'tasks': [base_task]}, actor='landing')
        return {'script': (script, qs), 'origin': (origin_task, qo), 'dirty': (dirty_task, qd), 'base': (base_task, qb)}

    def test_every_key_and_malformed_case(self):
        self.converter()
        valid = self.seed_question_keys()
        trusted = self.task(status='needs-owner', repo='trusted')
        qt = self.question(trusted, text='.drover/scripts/test.ps1')
        self.setting('_gate_question_trusted', str(qt))
        self.setting('gate_cmd_trusted', {'cmd': '.drover/scripts/test.ps1', 'question': qt, 'actor': 'owner'})
        bad_keys = []
        for n, value in enumerate(('', 'nonnumeric', '999', '{"question":false}', '{broken', '{}')):
            repo = f'bad{n}'
            self.task(status='needs-owner', repo=repo)
            for prefix in ('_gate_question_', '_origin_pause_', '_land_dirty_q_', 'gate_cmd_'):
                key = prefix + repo
                self.setting(key, value)
                if value:
                    bad_keys.append(key)
        for n, (text, actor, stored) in enumerate([
            ('../escape.ps1', 'owner', '../escape.ps1'),
            ('.drover/scripts/test.ps1', 'dispatcher', '.drover/scripts/test.ps1'),
            ('.drover/scripts/test.ps1', 'owner', '.drover/scripts/other.ps1')]):
            repo = f'forged{n}'
            tid = self.task(status='needs-owner', repo=repo)
            q = self.question(tid, text=text, actor=actor)
            self.setting('_gate_question_' + repo, str(q))
            self.setting('gate_cmd_' + repo, {'cmd': stored, 'question': q, 'actor': 'owner'})
        duplicate = self.question(valid['base'][0], options=['Retry landing', 'Keep holding'])
        self.event('base-red', {'sha': 'a' * 40, 'question': duplicate, 'tasks': [valid['base'][0]]}, actor='landing')
        tuner_epic = self.epic()
        self.setting('tuner_epic', tuner_epic)
        legacy_proposal = self.question(tuner_epic, kind='epic', title='Unknown old proposal', options=['Leave as is'])
        report, w = self.migrate()
        for key in bad_keys:
            self.assertTrue(any(d['location'] == 'settings/' + key for d in report['diagnostics']), key)
        for prefix in ('_gate_question_', '_origin_pause_', '_land_dirty_q_', 'gate_cmd_'):
            self.assertFalse(self.conn.execute('SELECT 1 FROM settings WHERE key LIKE ?', (prefix + '%',)).fetchone())
        self.assertIsNone(ops.get_setting(self.conn, 'landing_hold'))
        self.assertIsNone(ops.get_setting(self.conn, 'landing_hold_question'))
        self.assertEqual(w.answer(self.key('script', 'trusted'))['text'], '.drover/scripts/test.ps1')
        for n in range(3):
            self.assertIsNone(w.answer(self.key('script', f'forged{n}')))
        self.assertEqual(ops.get_question(self.conn, duplicate)['status'], 'answered')
        self.assertEqual(ops.get_question(self.conn, valid['base'][1])['status'], 'open')
        self.assertEqual(ops.get_setting(self.conn, 'tuner_epic'), tuner_epic)
        self.assertIsNotNone(w.state(self.key('tuner', 'legacy', legacy_proposal)))

    def test_historical_audit_not_reparked(self):
        self.converter()
        historical = self.task()
        self.event('replan-request', {}, task=historical)
        self.event('status', {'from': 'blocked', 'to': 'ready'}, task=historical, actor='owner')
        terminal = self.task(status='cancelled')
        self.event('replan-request', {}, task=terminal)
        active = self.task(status='running')
        self.event('replan-request', {}, task=active)
        self.conn.execute('INSERT INTO runs(task_id,role,backend,model,started) VALUES(?,?,?,?,?)',
                          (active, 'worker', 'codex', 'gpt-6-luna', STAMP))
        answered = self.task()
        qa = self.question(answered, options=['Approve borrowing', 'Keep default seat'], answer='Keep default seat')
        self.event('director-borrow', {'seat': 'codex', 'question': qa}, task=answered, actor='dispatcher')
        self.event('status', {'from': 'needs-owner', 'to': 'ready'}, task=answered, actor='owner')
        current = self.task(status='needs-owner')
        qc = self.question(current, options=['Approve borrowing', 'Keep default seat'])
        self.event('director-borrow', {'seat': 'codex', 'question': qc}, task=current, actor='dispatcher')
        report, w = self.migrate()
        for tid in (historical, terminal, answered):
            self.assertEqual(w.pending(('task', tid)), [])
        self.assertEqual({r['task_id'] for r in w.replans()}, {active})
        self.assertEqual(ops.get_task(self.conn, active)['status'], 'running')
        self.assertEqual(w.pending(('task', current))[0]['question_id'], qc)
        self.assertEqual(w.answer(self.key('borrow', answered, 'codex'))['choice'], 'Keep default seat')

    def test_legacy_tuner_identity_and_acknowledged_dedup(self):
        from drover import tuner
        epic = self.epic()
        self.setting('tuner_epic', epic)
        for name, answer in [('route_open', None), ('route_answered', 'Leave as is')]:
            self.question(epic, kind='epic', title=f'Review criteria text for {name} (criteria v1)?',
                          options=['Run a Sonnet criteria review', 'Leave as is'], answer=answer)
            self.conn.execute('INSERT INTO decisions(question_name,criteria_version,model,state_hash,answer,at) VALUES(?,?,?,?,?,?)',
                              (name, 'v1', 'gpt-6-luna', 'fixture', '{}', STAMP))
        report, w = self.migrate()
        for name in ('route_open', 'route_answered'):
            self.assertIsNotNone(w.state(self.key('tuner', 'criteria', name, 'v1')))
            self.assertEqual(tuner.proposals(self.conn, {name: {'version': 'v1', 'n': 20, 'rule_acc': None, 'model_acc': .5}}, {}), [])
        self.assertEqual(report['diagnostics'], [])

    def test_tuner_partial_identity_suppresses_until_acknowledged(self):
        from drover import tuner
        epic = self.epic()
        self.setting('tuner_epic', epic)
        q = self.question(epic, kind='epic', title='Review criteria text for uncertain (criteria v9)?',
                          options=['Run a Sonnet criteria review', 'Leave as is'])
        report, w = self.migrate()
        modern = self.key('tuner', 'criteria', 'uncertain', 'v9')
        self.assertEqual(w.state(modern), 'open')
        self.assertFalse(self.conn.execute('SELECT 1 FROM wait_causes WHERE cause=?', (modern,)).fetchone())
        self.assertTrue(any(d['raw'].get('known_identity') == ['criteria', 'uncertain', 'v9'] for d in report['diagnostics']))
        acc = {'uncertain': {'version': 'v9', 'n': 20, 'rule_acc': None, 'model_acc': .5}}
        self.assertEqual(tuner.proposals(self.conn, acc, {}), [])
        ops.answer_question(self.conn, q, choice='Leave as is')
        self.assertIsNone(w.state(modern))
        self.assertEqual(len(tuner.proposals(self.conn, acc, {})), 1)

    def test_all_legacy_tuner_grammars_restore_identity(self):
        epic = self.epic()
        self.setting('tuner_epic', epic)
        self.conn.execute('INSERT INTO decisions(question_name,criteria_version,model,state_hash,answer,at) VALUES(?,?,?,?,?,?)',
                          ('sample', 'v2', 'gpt-6-luna', 'fixture', '{}', STAMP))
        identities = [
            (['validate', 'sample', 'v2'], 'Validate sample (criteria v2): no rule baseline', ['Start validation', 'Not now']),
            (['promote', 'sample', 'v2'], 'Promote sample (criteria v2) from shadow to live?', ['Promote to live', 'Keep shadow']),
            (['criteria', 'sample', 'v2'], 'Review criteria text for sample (criteria v2)?', ['Run a Sonnet criteria review', 'Leave as is']),
            (['pin', 'worker:S', 'codex', 'gpt-6-luna'], 'Pin a rule: stop routing worker:S to gpt-6-luna?', ['Pin the rule', 'Keep routing']),
            (['brief', 'worker:M', 'codex', 'gpt-6-luna'], 'Review the brief template for worker:M on gpt-6-luna?', ['Run a Sonnet brief review', 'Leave as is'])]
        for _, title, options in identities:
            self.question(epic, kind='epic', title=title, options=options, answer=options[-1])
        report, w = self.migrate()
        self.assertEqual(report['diagnostics'], [])
        for identity, _, _ in identities:
            self.assertEqual(w.state(self.key('tuner', *identity)), 'resolved')

    def test_malformed_borrow_seat_is_diagnosed(self):
        tid = self.task(status='needs-owner')
        qid = self.question(tid, options=['Approve borrowing', 'Keep default seat'])
        eid = self.event('director-borrow', {'seat': [], 'question': qid}, task=tid, actor='dispatcher')
        report, w = self.migrate()
        self.assertTrue(any(d['location'] == f'task_events/{eid}' and d['code'] == 'invalid-borrow' for d in report['diagnostics']))
        self.assertTrue(w.pending(('task', tid)))

    def test_script_pair_order_and_task_provenance(self):
        tid = self.task(status='needs-owner')
        q = self.question(tid, text='.drover/scripts/test.ps1')
        self.setting('gate_cmd_p', {'cmd': '.drover/scripts/test.ps1', 'question': q, 'actor': 'owner'})
        self.setting('_gate_question_p', str(q))
        epic = self.epic('unrelated')
        bad = self.question(epic, kind='epic', text='.drover/scripts/unrelated.ps1')
        self.setting('_gate_question_other', str(bad))
        self.setting('gate_cmd_other', {'cmd': '.drover/scripts/unrelated.ps1', 'question': bad, 'actor': 'owner'})
        report, w = self.migrate()
        self.assertEqual(w.pending(('task', tid)), [])
        self.assertEqual(w.answer(self.key('script', 'p'))['text'], '.drover/scripts/test.ps1')
        self.assertFalse(any(d['location'] in ('settings/gate_cmd_p', f'tasks/{tid}/blocker') for d in report['diagnostics']))
        self.assertIsNone(w.answer(self.key('script', 'other')))
        self.assertTrue(any(d['location'] == 'settings/_gate_question_other' for d in report['diagnostics']))

    def test_later_base_resolution_is_not_reparked(self):
        for repo, kind, sha in [('moved', 'base-observed', 'b' * 40), ('green', 'base-ci', 'a' * 40)]:
            tid = self.task(status='reviewed', repo=repo)
            q = self.question(tid, options=['Retry landing', 'Keep holding'])
            self.event('base-red', {'sha': 'a' * 40, 'question': q, 'tasks': [tid]}, actor='landing')
            self.event(kind, {'repo': repo, 'sha': sha, **({'status': 'green'} if kind == 'base-ci' else {})}, actor='landing')
        _, w = self.migrate()
        self.assertEqual(w.pending(('merge_queue', 'moved')), [])
        self.assertEqual(w.pending(('merge_queue', 'green')), [])
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_migrated_question_release(self):
        self.converter()
        entries = self.seed_question_keys()
        urgent_source = self.task()
        urgent = self.task(status='blocked', blocker={'source': urgent_source, 'reason': 'urgent', 'prev': 'ready'})
        ordinary = self.task(status='needs-owner')
        ordinary_q = self.question(ordinary)
        report, w = self.migrate()
        for family, choice in [('script', None), ('origin', 'Retry origin sync'), ('dirty', 'Retry landing'), ('base', 'Retry landing')]:
            tid, q = entries[family]
            ops.answer_question(self.conn, q, choice=choice, text='.drover/scripts/test.ps1' if family == 'script' else None)
            self.assertEqual(w.pending(('task', tid)), [])
            if family in ('origin', 'dirty', 'base'):
                repo = {'origin': 'offline', 'dirty': 'dirty', 'base': 'base'}[family]
                cause = self.key(family, repo, 'a' * 40) if family == 'base' else self.key(family, repo)
                self.assertTrue(w.consume_retry(cause))
                self.assertFalse(w.consume_retry(cause))
        ops.event(self.conn, 'origin-sync-failed', 'dispatcher', {'repo': 'offline', 'reason': 'offline'})
        self.assertEqual(ops.get_question(self.conn, entries['origin'][1])['status'], 'open')
        uq = w.pending(('task', urgent))[0]['question_id']
        ops.answer_question(self.conn, uq, choice='Resume work')
        self.assertEqual(ops.get_task(self.conn, urgent)['status'], 'ready')
        ops.answer_question(self.conn, ordinary_q, text='Resume')
        self.assertEqual(ops.get_task(self.conn, ordinary)['status'], 'ready')


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
        self.assertEqual(db.schema_version(self.conn), 7)

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
