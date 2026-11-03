"""Frozen traces of how tasks move today: statuses, claims, holds, outside landing and accounting.

Each scenario drives the public ledger operations on an in-memory ledger and compares what they left behind (the
event log in emission order, the task, run and wait rows, and what each refusal did) with tests/fixtures/
lifecycle-traces.json. Timestamps are dropped and generated ids become aliases in order of first appearance, so
the trace is identical on Windows and WSL. A scenario records current behaviour; where that looks wrong the test
says so in a comment and keeps the behaviour. Regenerate with DROVER_WRITE_TRACES=1 and review the diff.

The negative half of the stage contract (a token's success assertion or a posted run observation cannot advance a
task) is already proved by C1_PROOFS below and is only referenced here.
"""
import ast
import datetime
import json
import os
import re
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import ci, contract, directors
from drover.ledger import db, ops
from drover.ledger.ops_stages import advance_stage, land_stage
from drover.ledger.states import IllegalTransition
from drover.ledger.waiting import TASK_TERMINAL, Waiting, cause_key
from drover.notifications import Notifications
from drover.port import DirectPort
from tests.fakeci import git_answers

FIXTURE = Path(__file__).parent / 'fixtures' / 'lifecycle-traces.json'
WAITING_KEY = 'nested_event_release_order'      # produced by tests/test_waiting_characterization.py
SHA, TIP = 'a' * 40, 'b' * 40
C1_PROOFS = (('test_authority_http', 'AuthorityHTTP', 'test_owner_and_shared_token_cannot_assert_engine_stages'),
             ('test_authority_slice', 'AuthorityTests', 'test_generic_stages_refused_and_evidence_advances'),
             ('test_authority_slice', 'AuthorityTests', 'test_running_stage_rejects_a_foreign_or_nonworker_run'))

DROPPED = {'at', 'created', 'updated', 'started', 'ended', 'wall_s', 'answered_at', 'created_at', 'id', 'timeout_at'}
ALIASED = {'run': 'run', 'gate_run': 'run', 'worker_run': 'run', 'retry_after_run': 'run',
           'wait': 'wait', 'question': 'question', 'request': 'request'}
TASK_ID = re.compile(r'\b[TE]-[0-9a-z]+\b')
TIME = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')
TASK_COLUMNS = ('title', 'status', 'kind', 'write', 'priority', 'size', 'rank', 'repo', 'scope_files', 'branch',
                'worktree', 'owner_kind', 'model', 'attempts', 'archived', 'internal', 'retry_after_run',
                'replans_used', 'epic_id', 'parent_id', 'blocker')
RUN_COLUMNS = ('task_id', 'role', 'backend', 'model', 'outcome', 'review_verdict', 'note')


def memory_ledger():
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    db.migrate(conn)
    return conn


ROW_LISTS = ('events', 'tasks', 'runs', 'waits', 'wait_subjects', 'landed')


def render(value, depth=0, key=None):
    """JSON with one event or row per line, so a changed trace reads as a short diff."""
    pad = ' ' * (depth + 1)
    if key in ROW_LISTS and value:
        return '[\n' + ',\n'.join(pad + json.dumps(v, sort_keys=True) for v in value) + '\n' + ' ' * depth + ']'
    if not isinstance(value, (dict, list)) or not value:
        return json.dumps(value, sort_keys=True)
    if isinstance(value, dict):
        body = [f'{pad}{json.dumps(k)}: {render(v, depth + 1, k)}' for k, v in sorted(value.items())]
        return '{\n' + ',\n'.join(body) + '\n' + ' ' * depth + '}'
    return '[\n' + ',\n'.join(pad + render(v, depth + 1) for v in value) + '\n' + ' ' * depth + ']'


def check_trace(case, name, trace):
    """Compare a trace with its frozen copy, or refreeze it when DROVER_WRITE_TRACES=1."""
    trace = json.loads(json.dumps(trace))
    if os.environ.get('DROVER_WRITE_TRACES') == '1':
        frozen = json.loads(FIXTURE.read_text('utf-8')) if FIXTURE.exists() else {}
        frozen[name] = trace
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(render(frozen) + '\n', 'utf-8', newline='\n')
        return
    case.assertEqual(json.loads(FIXTURE.read_text('utf-8'))[name], trace)


class Recorder:
    """Normalised view of one ledger: events since the last step, plus snapshots of the rows."""

    def __init__(self, conn, case):
        self.conn = conn
        self.assertion = case
        self.aliases = {}
        self.seen = 0
        self.steps = []

    def alias(self, kind, value):
        return self.aliases.setdefault((kind, value), f'{kind}{sum(1 for k in self.aliases if k[0] == kind) + 1}')

    def norm(self, value, key=None):
        if isinstance(value, dict):
            out = {}
            for k in sorted(value):
                if k in DROPPED:
                    continue
                if k == 'result_hash':      # a digest over rows that carry timestamps
                    out[k] = '<sha256>'
                    continue
                out[k] = (value[k] is not None) if k == 'approved_at' else self.norm(value[k], k)
            return out
        if isinstance(value, (list, tuple)):
            return [self.norm(v) for v in value]
        if isinstance(value, int) and not isinstance(value, bool) and key in ALIASED and value:
            return self.alias(ALIASED[key], value)
        if isinstance(value, str):
            value = TASK_ID.sub(lambda m: self.alias('task' if m.group().startswith('T') else 'epic', m.group()), value)
            return TIME.sub('<time>', value)
        return value

    def events(self):
        rows = ops.events(self.conn, after=self.seen)
        self.seen = rows[-1]['id'] if rows else self.seen
        return [dict(kind=r['kind'], actor=r['actor'], task=self.norm(r['task_id']), epic=self.norm(r['epic_id']),
                     data=self.norm(r['data'])) for r in rows]

    def step(self, label, result=None, run=None):
        step = {'step': label, 'events': self.events()}
        if result is not None:
            step['result'] = self.norm(result)
        if run is not None:
            step['run'] = self.alias('run', run)
        self.steps.append(step)

    def refused(self, label, error, call, *args, **kwargs):
        """Record a refusal and assert it changed nothing: no event, no task, run or wait row."""
        before = self.rows()
        with self.assertion.assertRaises(error) as caught:
            call(*args, **kwargs)
        self.assertion.assertEqual(self.rows(), before)
        self.steps.append({'step': label, 'refused': type(caught.exception).__name__,
                           'message': self.norm(str(caught.exception)), 'events': self.events()})

    def rows(self):
        c = self.conn
        tasks = [self.norm({**{k: r[k] for k in ('id', *TASK_COLUMNS)}, 'approved_at': r['approved_at']})
                 for r in c.execute('SELECT * FROM tasks ORDER BY rowid')]
        runs = [self.norm({k: self.run_value(r, k) for k in RUN_COLUMNS + ('gate_receipt',)}) for r in c.execute('SELECT * FROM runs ORDER BY id')]
        waits = [self.norm({'kind': w['kind'], 'subject': [w['subject_kind'], w['subject_key']], 'cause': w['cause'],
                            'released': w['released_event'] is not None, 'data': json.loads(w['data'])})
                 for w in c.execute('SELECT * FROM waits ORDER BY id')]
        subjects = [self.norm({'subject': [s['subject_kind'], s['subject_key']], 'resume_state': s['resume_state'],
                               'parked': s['parked'], 'reset_external': s['reset_external']})
                    for s in c.execute('SELECT * FROM wait_subjects ORDER BY rowid')]
        landed = [self.norm({k: r[k] for k in ('task_id', 'land_sha', 'tip', 'receipt')})
                  for r in c.execute('SELECT * FROM task_landed ORDER BY rowid')]
        return dict(tasks=tasks, runs=runs, waits=waits, wait_subjects=subjects, landed=landed)

    @staticmethod
    def run_value(row, key):
        return json.loads(row[key]) if key == 'gate_receipt' and row[key] else row[key]

    def trace(self):
        return dict(steps=self.steps, final=self.rows())


class Case(unittest.TestCase):
    def setUp(self):
        self.conn = memory_ledger()
        self.addCleanup(self.conn.close)
        self.rec = Recorder(self.conn, self)

    def task(self, title, ready=True, **kw):
        kw.setdefault('scope_files', [f'files/{title}'])
        task = ops.create_task(self.conn, title, 'p', **kw)['id']
        if ready:
            ops.approve(self.conn, task)
        return task

    def status(self, task):
        return ops.get_task(self.conn, task)['status']

    def gated_review(self, task):
        """Worker run through gate and review, one step per ledger operation; returns (worker, review)."""
        rec, c = self.rec, self.conn
        worker = ops.start_run(c, task, 'worker', 'local')
        rec.step('start worker', run=worker)
        ops.event(c, 'worker-done', 'dispatcher', {'run': worker, 'sha': SHA}, task_id=task)
        ops.finish_run(c, worker, 'done')
        rec.step('worker done', run=worker)
        gate = ops.start_run(c, task, 'gate', 'local')
        ops.event(c, 'gate-started', 'dispatcher', {'run': worker, 'gate_run': gate, 'sha': SHA, 'platform': 'windows'}, task_id=task)
        ops.finish_run(c, gate, 'passed')
        ops.amend_run(c, worker, outcome='gated', gate_receipt=contract.gate_receipt(True, receipt={'sha': SHA, 'status': 'passed'}))
        rec.step('gate passed', run=gate)
        advance_stage(c, task, 'gated', worker, SHA, platforms=['windows'])
        rec.step('advance gated')
        review = ops.start_run(c, task, 'review', 'local')
        ops.event(c, 'review-started', 'dispatcher', {'run': review, 'worker_run': worker, 'sha': SHA}, task_id=task)
        ops.finish_run(c, review, 'land', review_verdict='LAND@' + SHA)
        rec.step('review land', run=review)
        advance_stage(c, task, 'reviewed', review, SHA)
        rec.step('advance reviewed')
        return worker, review


class LifecycleTrace(Case):
    def test_approve_claim_start_finish_land(self):
        rec, c = self.rec, self.conn
        task = ops.create_task(c, 'Land me', 'p', scope_files=['src/land'], priority='P1', size='S')['id']
        rec.step('create', task)
        ops.approve(c, task)
        rec.step('approve')
        claimed = ops.claim_next(c, {'owner_kind': 'claude', 'model': 'sonnet', 'branch': 'drover/land', 'worktree': 'wt-land', 'actor': 'dispatcher'})
        rec.step('claim', claimed['id'])
        worker, review = self.gated_review(task)
        land_stage(c, {'id': task, 'head': SHA, 'last_sha': TIP}, TIP, {'version': ci.VERSION, 'passed': True, 'sha': TIP, 'log': 'merge gate'})
        rec.step('land')
        rec.refused('land again', ValueError, land_stage, c, {'id': task, 'head': SHA, 'last_sha': TIP}, TIP,
                    {'version': ci.VERSION, 'passed': True, 'sha': TIP, 'log': 'merge gate'})
        rec.refused('advance after landing', IllegalTransition, advance_stage, c, task, 'reviewed', review, SHA)
        rec.refused('generic claimed', IllegalTransition, ops.set_status, c, task, 'claimed', 'dispatcher')
        # Observation: every stage after claim is written with a distinct actor (worker, dispatcher, system), and
        # the status event of the worker start is attributed to the run's role, not to the dispatcher.
        check_trace(self, 'approve_claim_start_finish_land', rec.trace())

    def test_owner_findings_outside_and_manual_holds(self):
        rec, c = self.rec, self.conn
        read = self.task('findings', kind='research')
        ops.claim_next(c, {'actor': 'dispatcher'})
        worker = ops.start_run(c, read, 'worker', 'codex')
        ops.finish_run(c, worker, 'read-only-done', note='Findings ready')
        ops.set_status(c, read, 'needs-owner', 'dispatcher', reason='Review findings')
        rec.step('read-only findings ready', read)
        rec.refused('accept as dispatcher', PermissionError, ops.accept_read_only, c, read, actor='dispatcher')
        rec.refused('generic landed', IllegalTransition, ops.set_status, c, read, 'landed', 'owner')
        ops.accept_read_only(c, read)
        rec.step('owner accepts findings')
        rec.refused('accept again', ValueError, ops.accept_read_only, c, read)
        write = self.task('implementation')
        rec.step('second task ready', write)
        rec.refused('findings shortcut for a write task', ValueError, ops.accept_read_only, c, write)

        needs = self.task('elsewhere')
        ops.set_status(c, needs, 'needs-owner', 'worker', reason='landed elsewhere')
        rec.step('worker hands over', needs)
        evidence = (SHA, 'origin/main', 'gate passed', 'review approved')
        prog = {'name': 'p', 'repo': '/repo', 'default_branch': 'main', 'remote': 'origin', 'landing': {'push': True}}
        git = lambda repo, *args, **kw: SimpleNamespace(returncode=0, stdout={'cat-file': 'commit\n', 'rev-parse': TIP + '\n'}.get(args[0], ''))
        with mock.patch('drover.repos.load_all', return_value={'p': prog}), git_answers(git):
            rec.refused('outside by worker', PermissionError, ops.record_external_landing, c, needs, *evidence, actor='worker')
            rec.refused('outside by short sha', ValueError, ops.record_external_landing, c, needs, 'abc', *evidence[1:])
            rec.refused('outside on a ready task', IllegalTransition, ops.record_external_landing, c, write, *evidence)
            ops.record_external_landing(c, needs, *evidence)
            rec.step('owner records outside landing')
            ops.record_external_landing(c, needs, *evidence)
            rec.step('same evidence again')
            rec.refused('different evidence', IllegalTransition, ops.record_external_landing, c, needs, SHA, 'origin/main', 'other gate', 'review approved')

        held, source = self.task('held'), self.task('source')
        ops.block(c, held, 'owner has local work', actor='owner')
        rec.step('block', held)
        passed_over = ops.claim_next(c, {'actor': 'dispatcher'})
        rec.step('claim passes over the hold', passed_over['title'])
        rec.refused('blocked needs a reason', ValueError, ops.set_status, c, source, 'blocked', 'owner')
        ops.block(c, passed_over['id'], 'pause a claimed task', actor='owner')
        rec.step('hold on a claimed task', passed_over['id'])
        ops.unblock(c, passed_over['id'], actor='owner')
        rec.step('release returns a parked claimed task to ready, not claimed')
        waiting = Waiting(c, 'owner')
        waiting.wait(('task', held), TASK_TERMINAL, cause_key(source), cause=cause_key('task', source),
                     data={'source': source, 'mode': 'blocked'})
        rec.step('independent wait on source')
        ops.unblock(c, held, actor='owner')
        rec.step('unblock releases only the manual hold')
        rec.refused('unblock a task that is not held', IllegalTransition, ops.unblock, c, source, actor='owner')
        ops.set_status(c, source, 'cancelled', actor='owner')
        rec.step('source cancelled releases the other wait')
        # Observation: the task returns to ready with approved_at intact, and no extra approval event is written.
        draft = self.task('draft-hold', ready=False)
        ops.block(c, draft, 'park the proposal', actor='owner')
        ops.unblock(c, draft, actor='owner')
        rec.step('proposed task held and released', draft)
        check_trace(self, 'owner_findings_outside_and_manual_holds', rec.trace())

    def test_claim_order_scope_and_deps(self):
        rec, c = self.rec, self.conn
        now = ops.clock().replace(microsecond=0)
        ago = lambda hours: ops.stamp(now - datetime.timedelta(hours=hours))
        epic = ops.create_epic(c, 'p', 'ranked epic')['id']
        specs = [('low', dict(priority='P3')), ('in-epic', dict(epic_id=epic)), ('aged', {}), ('pressed', {}),
                 ('small', dict(size='S')), ('large', dict(size='L')), ('twin-a', {}), ('twin-b', {}),
                 ('src-wide', dict(scope_files=['src/*'])), ('src-one', dict(scope_files=['src/one'])),
                 ('needs-first', {}), ('waits-for', {}), ('urgent', dict(priority='P0'))]
        ids = {}
        for title, kw in specs:
            ids[title] = self.task(title, **kw)
            c.execute('UPDATE tasks SET rank=1, created=? WHERE id=?', (ago(10), ids[title]))
        c.execute('UPDATE tasks SET created=? WHERE id=?', (ago(2), ids['pressed']))
        c.execute('UPDATE tasks SET rank=2 WHERE id=?', (ids['twin-a'],))   # equal to twin-b in all else, but ranked later
        for n in range(9):
            follower = self.task(f'follower{n}', ready=False)
            ops.add_dep(c, follower, ids['pressed'])
        ops.add_dep(c, ids['needs-first'], ids['urgent'])
        ops.add_dep(c, ids['waits-for'], ids['urgent'], 'waits-for')
        rec.events()
        rec.step('ranked backlog', ids)
        excluded = ops.claim_next(c, {'actor': 'dispatcher'}, now=now, exclude=[ids['urgent']])
        rec.step('claim with the best task excluded', excluded['title'])
        order = [excluded['title']]
        for n in range(len(specs) + 1):
            got = ops.claim_next(c, {'actor': 'dispatcher'}, now=now)
            if got is None:
                break
            order.append(got['title'])
            rec.step(f'claim {n + 1}', got['title'])
        rec.step('claim order', order)
        rec.step('skipped while their scope or dependency is active', [t for t in ids if self.status(ids[t]) == 'ready'])
        check_trace(self, 'claim_order_scope_and_deps', rec.trace())

    def test_claim_tiebreak_is_task_id_text(self):
        """Equal priority, rank, pressure and size: T-1, T-10, T-11, T-2, not creation order (T-10 is the 37th task)."""
        rec, c = self.rec, self.conn
        now = ops.clock().replace(microsecond=0)
        ago = lambda hours: ops.stamp(now - datetime.timedelta(hours=hours))
        first, second = self.task('first'), self.task('second')
        ops.set_setting(c, 'seq_T', '35')
        third, fourth = self.task('third'), self.task('fourth')
        followers = [self.task(f'follower{n}', ready=False) for n in range(2)]
        ops.add_dep(c, followers[0], first)
        ops.add_dep(c, followers[1], third)
        # Pressure is hours x (1 + dependents) = 10 for all four, but `created` differs, so ordering by age would show.
        for task, hours in ((first, 5), (second, 10), (third, 5), (fourth, 10)):
            c.execute('UPDATE tasks SET rank=1, created=? WHERE id=?', (ago(hours), task))
        rec.events()
        rec.step('four equal tasks', {'first': first, 'second': second, 'third': third, 'fourth': fourth})
        order = []
        for n in range(5):
            got = ops.claim_next(c, {'actor': 'dispatcher'}, now=now)
            if got is None:
                break
            order.append(got['id'])
            rec.step(f'claim {n + 1}', got['title'])
        rec.step('claim order by id', order)
        # Observation: the tiebreak is the id compared as text, so a task created 36 tasks later is claimed first.
        check_trace(self, 'claim_tiebreak_is_task_id_text', rec.trace())

    def test_rollback_leaves_no_event_or_row(self):
        rec, c = self.rec, self.conn
        task, source = self.task('rolled'), self.task('rolled-source')
        rec.step('two ready tasks')

        def boom_after(operation):
            with self.assertRaises(RuntimeError):
                with ops.tx(c):
                    operation()
                    raise RuntimeError('late failure')

        before = rec.rows()
        boom_after(lambda: ops.claim_next(c, {'actor': 'dispatcher'}))
        boom_after(lambda: ops.block(c, task, 'held then lost', actor='owner'))
        boom_after(lambda: ops.set_status(c, task, 'draft', 'owner'))
        self.assertEqual(rec.rows(), before)
        rec.step('outer failure rolls back claim, hold and status')
        waiting = Waiting(c, 'owner')
        waiting.wait(('task', task), TASK_TERMINAL, cause_key(source), cause=cause_key('task', source),
                     data={'source': source, 'mode': 'blocked'})
        rec.step('wait registered', task)
        rec.refused('release of an unsatisfied cause', ValueError, waiting.release, cause_key('task', source))
        rec.refused('owner readies a task with an independent wait', ValueError, ops.set_status, c, task, 'ready', 'owner')
        check_trace(self, 'rollback_leaves_no_event_or_row', rec.trace())

    def test_internal_accounting_task_is_created_once_and_immutable(self):
        rec, c = self.rec, self.conn
        service = Notifications(c, now=lambda: 1000.)
        ops.set_setting(c, 'dispatch_enabled', 'true')
        ops.set_setting(c, 'director_execution_enabled', 'true')
        ops.set_setting(c, 'director_execution', json.dumps({'director:a': {
            'backend': 'claude', 'model': 'test-model', 'seat': 'fixture', 'max_tokens': 20,
            'placement': 'windows', 'cwd': 'unused'}}))
        service.register('director:a', allowed_models=['test-model'], token_budget=100,
                         wake_limit_per_hour=4, replacement_limit=1)
        launched = []
        adapter = SimpleNamespace(
            capabilities=dict(live_offer=False, verified_resume=False, fresh_start=True, enforced_token_cap=True,
                              authenticated_processing=True, secure_handoff=True),
            launch=lambda spec, credential, guard: launched.append(spec['run_id']) or SimpleNamespace(poll=lambda: None),
            probe=lambda spec: 'alive')
        runtime = directors.DirectorRuntime(DirectPort(c, now=lambda: 1000.), adapter, now=lambda: 1000.)
        child = ops.create_task(c, 'Child', 'repo', kind='research', initiating_role='director:a')['id']
        ops.event(c, 'task.result-accepted', 'owner', {'result': 'report'}, task_id=child)
        runtime.tick(service.admission(), {'slots': 2, 'admit': lambda spec: None})
        anchor = c.execute('SELECT * FROM tasks WHERE internal=1').fetchone()
        run = c.execute('SELECT * FROM runs WHERE role=?', ('director',)).fetchone()
        self.assertEqual(launched, [run['id']])
        self.assertEqual(run['task_id'], anchor['id'])
        created = [e for e in rec.events() if e['kind'] == 'director.accounting-created']
        rec.steps.append({'step': 'first director start', 'task': rec.norm(dict(
            {k: anchor[k] for k in ('id', *TASK_COLUMNS)}, approved_at=anchor['approved_at'])),
            'run': rec.norm({k: run[k] for k in RUN_COLUMNS}), 'events': created})
        a = anchor['id']
        rec.refused('status of the accounting task', PermissionError, ops.set_status, c, a, 'ready', 'owner')
        rec.refused('worker run on the accounting task', PermissionError, ops.start_run, c, a, 'worker', 'local')
        rec.refused('outside landing on the accounting task', PermissionError, ops.record_external_landing, c, a, SHA, 'origin/main', 'g', 'r')
        rec.refused('archive of the accounting task', PermissionError, ops.archive_task, c, a, False)
        rec.refused('delete of the accounting task', PermissionError, ops.delete_task, c, a, True)
        ops.add_comment(c, 'task', a, 'owner', 'hello')
        rec.step('comment on the accounting task')
        # Observation: the immutability guard does not cover comments; the owner's comment is accepted.
        # Observation: it is an archived draft, so the backlog and the claimer never see it.
        self.assertNotIn(a, [t['id'] for t in ops.list_tasks(c)])
        self.assertIsNone(ops.claim_next(c, {'actor': 'dispatcher'}))
        check_trace(self, 'internal_accounting_task_is_created_once_and_immutable', {'steps': rec.steps})

    def test_all_frozen_traces(self):
        """Every fixture entry has a test that consumes it, and every such test has an entry."""
        frozen = json.loads(FIXTURE.read_text('utf-8'))
        mine = {n[len('test_'):] for n in dir(LifecycleTrace) if n.startswith('test_')} - {
            'all_frozen_traces', 'c1_proofs_are_still_named'}
        self.assertEqual(set(frozen), mine | {WAITING_KEY})
        self.assertIn('test_' + WAITING_KEY, method_names('test_waiting_characterization', 'WaitingTrace'))
        for name, trace in frozen.items():
            self.assertTrue(trace.get('steps'), name)

    def test_c1_proofs_are_still_named(self):
        """The negative half (tokens and posted observations cannot reach the stage APIs) lives in these tests."""
        for module, cls, name in C1_PROOFS:
            self.assertIn(name, method_names(module, cls), f'{module}.{cls}')


def method_names(module, cls):
    """Method names of a test class, read from source so the module is not imported."""
    tree = ast.parse((Path(__file__).parent / f'{module}.py').read_text('utf-8'))
    return [f.name for c in tree.body if isinstance(c, ast.ClassDef) and c.name == cls
            for f in c.body if isinstance(f, ast.FunctionDef)]


if __name__ == '__main__':
    unittest.main()
