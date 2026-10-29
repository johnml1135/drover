"""Question association migration tests."""
from drover.ledger import ops
from tests.test_waiting_migration import MigrationCase, STAMP


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
